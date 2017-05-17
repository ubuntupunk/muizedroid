#!/usr/bin/env python3
#
# update.py - part of the FDroid server tools
# Copyright (C) 2017, Torsten Grote <t at grobox dot de>
# Copyright (C) 2016, Blue Jay Wireless
# Copyright (C) 2014-2016, Hans-Christoph Steiner <hans@eds.org>
# Copyright (C) 2010-2015, Ciaran Gultnieks <ciaran@ciarang.com>
# Copyright (C) 2013-2014, Daniel Martí <mvdan@mvdan.cc>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import collections
import copy
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
import zipfile
from binascii import hexlify, unhexlify
from datetime import datetime
from xml.dom.minidom import Document

from fdroidserver import metadata, signindex, common, net
from fdroidserver.common import FDroidPopen, FDroidPopenBytes
from fdroidserver.metadata import MetaDataException


def make(apps, sortedids, apks, repodir, archive):
    """Generate the repo index files.

    This requires properly initialized options and config objects.

    :param apps: fully populated apps list
    :param sortedids: app package IDs, sorted
    :param apks: full populated apks list
    :param repodir: the repo directory
    :param archive: True if this is the archive repo, False if it's the
                    main one.
    """
    from fdroidserver.update import METADATA_VERSION

    def _resolve_description_link(appid):
        if appid in apps:
            return "fdroid.app:" + appid, apps[appid].Name
        raise MetaDataException("Cannot resolve app id " + appid)

    nosigningkey = False
    if not common.options.nosign:
        if 'repo_keyalias' not in common.config:
            nosigningkey = True
            logging.critical("'repo_keyalias' not found in config.py!")
        if 'keystore' not in common.config:
            nosigningkey = True
            logging.critical("'keystore' not found in config.py!")
        if 'keystorepass' not in common.config:
            nosigningkey = True
            logging.critical("'keystorepass' not found in config.py!")
        if 'keypass' not in common.config:
            nosigningkey = True
            logging.critical("'keypass' not found in config.py!")
        if not os.path.exists(common.config['keystore']):
            nosigningkey = True
            logging.critical("'" + common.config['keystore'] + "' does not exist!")
        if nosigningkey:
            logging.warning("`fdroid update` requires a signing key, you can create one using:")
            logging.warning("\tfdroid update --create-key")
            sys.exit(1)

    repodict = collections.OrderedDict()
    repodict['timestamp'] = datetime.utcnow()
    repodict['version'] = METADATA_VERSION

    if common.config['repo_maxage'] != 0:
        repodict['maxage'] = common.config['repo_maxage']

    if archive:
        repodict['name'] = common.config['archive_name']
        repodict['icon'] = os.path.basename(common.config['archive_icon'])
        repodict['address'] = common.config['archive_url']
        repodict['description'] = common.config['archive_description']
        urlbasepath = os.path.basename(urllib.parse.urlparse(common.config['archive_url']).path)
    else:
        repodict['name'] = common.config['repo_name']
        repodict['icon'] = os.path.basename(common.config['repo_icon'])
        repodict['address'] = common.config['repo_url']
        repodict['description'] = common.config['repo_description']
        urlbasepath = os.path.basename(urllib.parse.urlparse(common.config['repo_url']).path)

    mirrorcheckfailed = False
    mirrors = []
    for mirror in sorted(common.config.get('mirrors', [])):
        base = os.path.basename(urllib.parse.urlparse(mirror).path.rstrip('/'))
        if common.config.get('nonstandardwebroot') is not True and base != 'fdroid':
            logging.error("mirror '" + mirror + "' does not end with 'fdroid'!")
            mirrorcheckfailed = True
        # must end with / or urljoin strips a whole path segment
        if mirror.endswith('/'):
            mirrors.append(urllib.parse.urljoin(mirror, urlbasepath))
        else:
            mirrors.append(urllib.parse.urljoin(mirror + '/', urlbasepath))
    for mirror in common.config.get('servergitmirrors', []):
        mirror = get_mirror_service_url(mirror)
        if mirror is not None:
            mirrors.append(mirror + '/')
    if mirrorcheckfailed:
        sys.exit(1)
    if mirrors:
        repodict['mirrors'] = mirrors

    appsWithPackages = collections.OrderedDict()
    for packageName in sortedids:
        app = apps[packageName]
        if app['Disabled']:
            continue

        # only include apps with packages
        for apk in apks:
            if apk['packageName'] == packageName:
                newapp = copy.copy(app)  # update wiki needs unmodified description
                newapp['Description'] = metadata.description_html(app['Description'],
                                                                  _resolve_description_link)
                appsWithPackages[packageName] = newapp
                break

    requestsdict = collections.OrderedDict()
    for command in ('install', 'uninstall'):
        packageNames = []
        key = command + '_list'
        if key in common.config:
            if isinstance(common.config[key], str):
                packageNames = [common.config[key]]
            elif all(isinstance(item, str) for item in common.config[key]):
                packageNames = common.config[key]
            else:
                raise TypeError('only accepts strings, lists, and tuples')
        requestsdict[command] = packageNames

    make_v0(appsWithPackages, apks, repodir, repodict, requestsdict)
    make_v1(appsWithPackages, apks, repodir, repodict, requestsdict)


def make_v1(apps, packages, repodir, repodict, requestsdict):

    def _index_encoder_default(obj):
        if isinstance(obj, set):
            return list(obj)
        if isinstance(obj, datetime):
            return int(obj.timestamp() * 1000)  # Java expects milliseconds
        raise TypeError(repr(obj) + " is not JSON serializable")

    output = collections.OrderedDict()
    output['repo'] = repodict
    output['requests'] = requestsdict

    appslist = []
    output['apps'] = appslist
    for appid, appdict in apps.items():
        d = collections.OrderedDict()
        appslist.append(d)
        for k, v in sorted(appdict.items()):
            if not v:
                continue
            if k in ('builds', 'comments', 'metadatapath',
                     'ArchivePolicy', 'AutoUpdateMode', 'MaintainerNotes',
                     'Provides', 'Repo', 'RepoType', 'RequiresRoot',
                     'UpdateCheckData', 'UpdateCheckIgnore', 'UpdateCheckMode',
                     'UpdateCheckName', 'NoSourceSince', 'VercodeOperation'):
                continue

            # name things after the App class fields in fdroidclient
            if k == 'id':
                k = 'packageName'
            elif k == 'CurrentVersionCode':  # TODO make SuggestedVersionCode the canonical name
                k = 'suggestedVersionCode'
            elif k == 'CurrentVersion':  # TODO make SuggestedVersionName the canonical name
                k = 'suggestedVersionName'
            elif k == 'AutoName':
                if 'Name' not in apps[appid]:
                    d['name'] = v
                continue
            else:
                k = k[:1].lower() + k[1:]
            d[k] = v

    output_packages = collections.OrderedDict()
    output['packages'] = output_packages
    for package in packages:
        packageName = package['packageName']
        if packageName in output_packages:
            packagelist = output_packages[packageName]
        else:
            packagelist = []
            output_packages[packageName] = packagelist
        d = collections.OrderedDict()
        packagelist.append(d)
        for k, v in sorted(package.items()):
            if not v:
                continue
            if k in ('icon', 'icons', 'icons_src', 'name', ):
                continue
            d[k] = v

    json_name = 'index-v1.json'
    index_file = os.path.join(repodir, json_name)
    with open(index_file, 'w') as fp:
        if common.options.pretty:
            json.dump(output, fp, default=_index_encoder_default, indent=2)
        else:
            json.dump(output, fp, default=_index_encoder_default)

    if common.options.nosign:
        logging.debug('index-v1 must have a signature, use `fdroid signindex` to create it!')
    else:
        signindex.config = common.config
        signindex.sign_index_v1(repodir, json_name)


def make_v0(apps, apks, repodir, repodict, requestsdict):
    """
    aka index.jar aka index.xml
    """

    doc = Document()

    def addElement(name, value, doc, parent):
        el = doc.createElement(name)
        el.appendChild(doc.createTextNode(value))
        parent.appendChild(el)

    def addElementNonEmpty(name, value, doc, parent):
        if not value:
            return
        addElement(name, value, doc, parent)

    def addElementIfInApk(name, apk, key, doc, parent):
        if key not in apk:
            return
        value = str(apk[key])
        addElement(name, value, doc, parent)

    def addElementCDATA(name, value, doc, parent):
        el = doc.createElement(name)
        el.appendChild(doc.createCDATASection(value))
        parent.appendChild(el)

    root = doc.createElement("fdroid")
    doc.appendChild(root)

    repoel = doc.createElement("repo")

    repoel.setAttribute("name", repodict['name'])
    if 'maxage' in repodict:
        repoel.setAttribute("maxage", str(repodict['maxage']))
    repoel.setAttribute("icon", os.path.basename(repodict['icon']))
    repoel.setAttribute("url", repodict['address'])
    addElement('description', repodict['description'], doc, repoel)
    for mirror in repodict.get('mirrors', []):
        addElement('mirror', mirror, doc, repoel)

    repoel.setAttribute("version", str(repodict['version']))
    repoel.setAttribute("timestamp", '%d' % repodict['timestamp'].timestamp())

    pubkey, repo_pubkey_fingerprint = extract_pubkey()
    repoel.setAttribute("pubkey", pubkey.decode('utf-8'))
    root.appendChild(repoel)

    for command in ('install', 'uninstall'):
        for packageName in requestsdict[command]:
            element = doc.createElement(command)
            root.appendChild(element)
            element.setAttribute('packageName', packageName)

    for appid, appdict in apps.items():
        app = metadata.App(appdict)

        if app.Disabled is not None:
            continue

        # Get a list of the apks for this app...
        apklist = []
        for apk in apks:
            if apk['packageName'] == appid:
                apklist.append(apk)

        if len(apklist) == 0:
            continue

        apel = doc.createElement("application")
        apel.setAttribute("id", app.id)
        root.appendChild(apel)

        addElement('id', app.id, doc, apel)
        if app.added:
            addElement('added', app.added.strftime('%Y-%m-%d'), doc, apel)
        if app.lastUpdated:
            addElement('lastupdated', app.lastUpdated.strftime('%Y-%m-%d'), doc, apel)
        addElement('name', app.Name, doc, apel)
        addElement('summary', app.Summary, doc, apel)
        if app.icon:
            addElement('icon', app.icon, doc, apel)

        if app.get('Description'):
            description = app.Description
        else:
            description = '<p>No description available</p>'
        addElement('desc', description, doc, apel)
        addElement('license', app.License, doc, apel)
        if app.Categories:
            addElement('categories', ','.join(app.Categories), doc, apel)
            # We put the first (primary) category in LAST, which will have
            # the desired effect of making clients that only understand one
            # category see that one.
            addElement('category', app.Categories[0], doc, apel)
        addElement('web', app.WebSite, doc, apel)
        addElement('source', app.SourceCode, doc, apel)
        addElement('tracker', app.IssueTracker, doc, apel)
        addElementNonEmpty('changelog', app.Changelog, doc, apel)
        addElementNonEmpty('author', app.AuthorName, doc, apel)
        addElementNonEmpty('email', app.AuthorEmail, doc, apel)
        addElementNonEmpty('donate', app.Donate, doc, apel)
        addElementNonEmpty('bitcoin', app.Bitcoin, doc, apel)
        addElementNonEmpty('litecoin', app.Litecoin, doc, apel)
        addElementNonEmpty('flattr', app.FlattrID, doc, apel)

        # These elements actually refer to the current version (i.e. which
        # one is recommended. They are historically mis-named, and need
        # changing, but stay like this for now to support existing clients.
        addElement('marketversion', app.CurrentVersion, doc, apel)
        addElement('marketvercode', app.CurrentVersionCode, doc, apel)

        if app.Provides:
            pv = app.Provides.split(',')
            addElementNonEmpty('provides', ','.join(pv), doc, apel)
        if app.RequiresRoot:
            addElement('requirements', 'root', doc, apel)

        # Sort the apk list into version order, just so the web site
        # doesn't have to do any work by default...
        apklist = sorted(apklist, key=lambda apk: apk['versionCode'], reverse=True)

        if 'antiFeatures' in apklist[0]:
            app.AntiFeatures.extend(apklist[0]['antiFeatures'])
        if app.AntiFeatures:
            addElementNonEmpty('antifeatures', ','.join(app.AntiFeatures), doc, apel)

        # Check for duplicates - they will make the client unhappy...
        for i in range(len(apklist) - 1):
            if apklist[i]['versionCode'] == apklist[i + 1]['versionCode']:
                logging.critical("duplicate versions: '%s' - '%s'" % (
                    apklist[i]['apkName'], apklist[i + 1]['apkName']))
                sys.exit(1)

        current_version_code = 0
        current_version_file = None
        for apk in apklist:
            file_extension = common.get_file_extension(apk['apkName'])
            # find the APK for the "Current Version"
            if current_version_code < apk['versionCode']:
                current_version_code = apk['versionCode']
            if current_version_code < int(app.CurrentVersionCode):
                current_version_file = apk['apkName']

            apkel = doc.createElement("package")
            apel.appendChild(apkel)
            addElement('version', apk['versionName'], doc, apkel)
            addElement('versioncode', str(apk['versionCode']), doc, apkel)
            addElement('apkname', apk['apkName'], doc, apkel)
            addElementIfInApk('srcname', apk, 'srcname', doc, apkel)

            hashel = doc.createElement("hash")
            hashel.setAttribute('type', 'sha256')
            hashel.appendChild(doc.createTextNode(apk['hash']))
            apkel.appendChild(hashel)

            addElement('size', str(apk['size']), doc, apkel)
            addElementIfInApk('sdkver', apk,
                              'minSdkVersion', doc, apkel)
            addElementIfInApk('targetSdkVersion', apk,
                              'targetSdkVersion', doc, apkel)
            addElementIfInApk('maxsdkver', apk,
                              'maxSdkVersion', doc, apkel)
            addElementIfInApk('obbMainFile', apk,
                              'obbMainFile', doc, apkel)
            addElementIfInApk('obbMainFileSha256', apk,
                              'obbMainFileSha256', doc, apkel)
            addElementIfInApk('obbPatchFile', apk,
                              'obbPatchFile', doc, apkel)
            addElementIfInApk('obbPatchFileSha256', apk,
                              'obbPatchFileSha256', doc, apkel)
            if 'added' in apk:
                addElement('added', apk['added'].strftime('%Y-%m-%d'), doc, apkel)

            if file_extension == 'apk':  # sig is required for APKs, but only APKs
                addElement('sig', apk['sig'], doc, apkel)

                old_permissions = set()
                sorted_permissions = sorted(apk['uses-permission'])
                for perm in sorted_permissions:
                    perm_name = perm.name
                    if perm_name.startswith("android.permission."):
                        perm_name = perm_name[19:]
                    old_permissions.add(perm_name)
                addElementNonEmpty('permissions', ','.join(sorted(old_permissions)), doc, apkel)

                for permission in sorted_permissions:
                    permel = doc.createElement('uses-permission')
                    permel.setAttribute('name', permission.name)
                    if permission.maxSdkVersion is not None:
                        permel.setAttribute('maxSdkVersion', '%d' % permission.maxSdkVersion)
                        apkel.appendChild(permel)
                for permission_sdk_23 in sorted(apk['uses-permission-sdk-23']):
                    permel = doc.createElement('uses-permission-sdk-23')
                    permel.setAttribute('name', permission_sdk_23.name)
                    if permission_sdk_23.maxSdkVersion is not None:
                        permel.setAttribute('maxSdkVersion', '%d' % permission_sdk_23.maxSdkVersion)
                        apkel.appendChild(permel)
                if 'nativecode' in apk:
                    addElement('nativecode', ','.join(sorted(apk['nativecode'])), doc, apkel)
                addElementNonEmpty('features', ','.join(sorted(apk['features'])), doc, apkel)

        if current_version_file is not None \
                and common.config['make_current_version_link'] \
                and repodir == 'repo':  # only create these
            namefield = common.config['current_version_name_source']
            sanitized_name = re.sub(b'''[ '"&%?+=/]''', b'', app.get(namefield).encode('utf-8'))
            apklinkname = sanitized_name + b'.apk'
            current_version_path = os.path.join(repodir, current_version_file).encode('utf-8', 'surrogateescape')
            if os.path.islink(apklinkname):
                os.remove(apklinkname)
            os.symlink(current_version_path, apklinkname)
            # also symlink gpg signature, if it exists
            for extension in (b'.asc', b'.sig'):
                sigfile_path = current_version_path + extension
                if os.path.exists(sigfile_path):
                    siglinkname = apklinkname + extension
                    if os.path.islink(siglinkname):
                        os.remove(siglinkname)
                    os.symlink(sigfile_path, siglinkname)

    if common.options.pretty:
        output = doc.toprettyxml(encoding='utf-8')
    else:
        output = doc.toxml(encoding='utf-8')

    with open(os.path.join(repodir, 'index.xml'), 'wb') as f:
        f.write(output)

    if 'repo_keyalias' in common.config:

        if common.options.nosign:
            logging.info("Creating unsigned index in preparation for signing")
        else:
            logging.info("Creating signed index with this key (SHA256):")
            logging.info("%s" % repo_pubkey_fingerprint)

        # Create a jar of the index...
        jar_output = 'index_unsigned.jar' if common.options.nosign else 'index.jar'
        p = FDroidPopen(['jar', 'cf', jar_output, 'index.xml'], cwd=repodir)
        if p.returncode != 0:
            logging.critical("Failed to create {0}".format(jar_output))
            sys.exit(1)

        # Sign the index...
        signed = os.path.join(repodir, 'index.jar')
        if common.options.nosign:
            # Remove old signed index if not signing
            if os.path.exists(signed):
                os.remove(signed)
        else:
            signindex.config = common.config
            signindex.sign_jar(signed)

    # Copy the repo icon into the repo directory...
    icon_dir = os.path.join(repodir, 'icons')
    iconfilename = os.path.join(icon_dir, os.path.basename(common.config['repo_icon']))
    shutil.copyfile(common.config['repo_icon'], iconfilename)


def extract_pubkey():
    """
    Extracts and returns the repository's public key from the keystore.
    :return: public key in hex, repository fingerprint
    """
    if 'repo_pubkey' in common.config:
        pubkey = unhexlify(common.config['repo_pubkey'])
    else:
        env_vars = {'FDROID_KEY_STORE_PASS': common.config['keystorepass']}
        p = FDroidPopenBytes([common.config['keytool'], '-exportcert',
                              '-alias', common.config['repo_keyalias'],
                              '-keystore', common.config['keystore'],
                              '-storepass:env', 'FDROID_KEY_STORE_PASS']
                             + common.config['smartcardoptions'],
                             envs=env_vars, output=False, stderr_to_stdout=False)
        if p.returncode != 0 or len(p.output) < 20:
            msg = "Failed to get repo pubkey!"
            if common.config['keystore'] == 'NONE':
                msg += ' Is your crypto smartcard plugged in?'
            logging.critical(msg)
            sys.exit(1)
        pubkey = p.output
    repo_pubkey_fingerprint = common.get_cert_fingerprint(pubkey)
    return hexlify(pubkey), repo_pubkey_fingerprint


def get_mirror_service_url(url):
    '''Get direct URL from git service for use by fdroidclient

    Via 'servergitmirrors', fdroidserver can create and push a mirror
    to certain well known git services like gitlab or github.  This
    will always use the 'master' branch since that is the default
    branch in git.

    '''

    if url.startswith('git@'):
        url = re.sub(r'^git@(.*):(.*)', r'https://\1/\2', url)

    segments = url.split("/")

    if segments[4].endswith('.git'):
        segments[4] = segments[4][:-4]

    hostname = segments[2]
    user = segments[3]
    repo = segments[4]
    branch = "master"
    folder = "fdroid"

    if hostname == "github.com":
        # Github-like RAW segments "https://raw.githubusercontent.com/user/repo/master/fdroid"
        segments[2] = "raw.githubusercontent.com"
        segments.extend([branch, folder])
    elif hostname == "gitlab.com":
        # Gitlab-like Pages segments "https://user.gitlab.com/repo/fdroid"
        gitlab_url = ["https:", "", user + ".gitlab.io", repo, folder]
        segments = gitlab_url
    else:
        return None

    return '/'.join(segments)


class VerificationException(Exception):
    pass


def download_repo_index(url_str, etag=None, verify_fingerprint=True):
    """
    Downloads the repository index from the given :param url_str
    and verifies the repository's fingerprint if :param verify_fingerprint is not False.

    :raises: VerificationException() if the repository could not be verified

    :return: A tuple consisting of:
        - The index in JSON format or None if the index did not change
        - The new eTag as returned by the HTTP request
    """
    url = urllib.parse.urlsplit(url_str)

    fingerprint = None
    if verify_fingerprint:
        query = urllib.parse.parse_qs(url.query)
        if 'fingerprint' not in query:
            raise VerificationException("No fingerprint in URL.")
        fingerprint = query['fingerprint'][0]

    url = urllib.parse.SplitResult(url.scheme, url.netloc, url.path + '/index-v1.jar', '', '')
    download, new_etag = net.http_get(url.geturl(), etag)

    if download is None:
        return None, new_etag

    with tempfile.NamedTemporaryFile() as fp:
        # write and open JAR file
        fp.write(download)
        jar = zipfile.ZipFile(fp)

        # verify that the JAR signature is valid
        verify_jar_signature(fp.name)

        # get public key and its fingerprint from JAR
        public_key, public_key_fingerprint = get_public_key_from_jar(jar)

        # compare the fingerprint if verify_fingerprint is True
        if verify_fingerprint and fingerprint.upper() != public_key_fingerprint:
            raise VerificationException("The repository's fingerprint does not match.")

        # load repository index from JSON
        index = json.loads(jar.read('index-v1.json').decode("utf-8"))
        index["repo"]["pubkey"] = hexlify(public_key).decode("utf-8")
        index["repo"]["fingerprint"] = public_key_fingerprint

        # turn the apps into App objects
        index["apps"] = [metadata.App(app) for app in index["apps"]]

        return index, new_etag


def verify_jar_signature(file):
    """
    Verifies the signature of a given JAR file.

    :raises: VerificationException() if the JAR's signature could not be verified
    """
    if not common.verify_apk_signature(file, jar=True):
        raise VerificationException("The repository's index could not be verified.")


def get_public_key_from_jar(jar):
    """
    Get the public key and its fingerprint from a JAR file.

    :raises: VerificationException() if the JAR was not signed exactly once

    :param jar: a zipfile.ZipFile object
    :return: the public key from the jar and its fingerprint
    """
    # extract certificate from jar
    certs = [n for n in jar.namelist() if common.CERT_PATH_REGEX.match(n)]
    if len(certs) < 1:
        raise VerificationException("Found no signing certificates for repository.")
    if len(certs) > 1:
        raise VerificationException("Found multiple signing certificates for repository.")

    # extract public key from certificate
    public_key = common.get_certificate(jar.read(certs[0]))
    public_key_fingerprint = common.get_cert_fingerprint(public_key).replace(' ', '')

    return public_key, public_key_fingerprint
