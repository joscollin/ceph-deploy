import argparse
import json
import logging
import os
import re
import sys
import time
from textwrap import dedent

from ceph_deploy import conf, exc, hosts, mon
from ceph_deploy.util import constants, system, packages
from ceph_deploy.cliutil import priority
from ceph_deploy.lib import remoto


LOG = logging.getLogger(__name__)


def get_bootstrap_osd_key(cluster):
    """
    Read the bootstrap-osd key for `cluster`.
    """
    path = '{cluster}.bootstrap-osd.keyring'.format(cluster=cluster)
    try:
        with open(path, 'rb') as f:
            return f.read()
    except IOError:
        raise RuntimeError('bootstrap-osd keyring not found; run \'gatherkeys\'')


def create_osd_keyring(conn, cluster, key):
    """
    Run on osd node, writes the bootstrap key if not there yet.
    """
    logger = conn.logger
    path = '/var/lib/ceph/bootstrap-osd/{cluster}.keyring'.format(
        cluster=cluster,
        )
    if not conn.remote_module.path_exists(path):
        logger.warning('osd keyring does not exist yet, creating one')
        conn.remote_module.write_keyring(path, key)


def osd_tree(conn, cluster):
    """
    Check the status of an OSD. Make sure all are up and in

    What good output would look like::

        {
            "epoch": 8,
            "num_osds": 1,
            "num_up_osds": 1,
            "num_in_osds": "1",
            "full": "false",
            "nearfull": "false"
        }

    Note how the booleans are actually strings, so we need to take that into
    account and fix it before returning the dictionary. Issue #8108
    """
    ceph_executable = system.executable_path(conn, 'ceph')
    command = [
        ceph_executable,
        '--cluster={cluster}'.format(cluster=cluster),
        'osd',
        'tree',
        '--format=json',
    ]

    out, err, code = remoto.process.check(
        conn,
        command,
    )

    try:
        loaded_json = json.loads(b''.join(out).decode('utf-8'))
        # convert boolean strings to actual booleans because
        # --format=json fails to do this properly
        for k, v in loaded_json.items():
            if v == 'true':
                loaded_json[k] = True
            elif v == 'false':
                loaded_json[k] = False
        return loaded_json
    except ValueError:
        return {}


def osd_status_check(conn, cluster):
    """
    Check the status of an OSD. Make sure all are up and in

    What good output would look like::

        {
            "epoch": 8,
            "num_osds": 1,
            "num_up_osds": 1,
            "num_in_osds": "1",
            "full": "false",
            "nearfull": "false"
        }

    Note how the booleans are actually strings, so we need to take that into
    account and fix it before returning the dictionary. Issue #8108
    """
    ceph_executable = system.executable_path(conn, 'ceph')
    command = [
        ceph_executable,
        '--cluster={cluster}'.format(cluster=cluster),
        'osd',
        'stat',
        '--format=json',
    ]

    try:
        out, err, code = remoto.process.check(
            conn,
            command,
        )
    except TypeError:
        # XXX This is a bug in remoto. If the other end disconnects with a timeout
        # it will return a None, and here we are expecting a 3 item tuple, not a None
        # so it will break with a TypeError. Once remoto fixes this, we no longer need
        # this try/except.
        return {}

    try:
        loaded_json = json.loads(b''.join(out).decode('utf-8'))
        # convert boolean strings to actual booleans because
        # --format=json fails to do this properly
        for k, v in loaded_json.items():
            if v == 'true':
                loaded_json[k] = True
            elif v == 'false':
                loaded_json[k] = False
        return loaded_json
    except ValueError:
        return {}


def catch_osd_errors(conn, logger, args):
    """
    Look for possible issues when checking the status of an OSD and
    report them back to the user.
    """
    logger.info('checking OSD status...')
    status = osd_status_check(conn, args.cluster)
    osds = int(status.get('num_osds', 0))
    up_osds = int(status.get('num_up_osds', 0))
    in_osds = int(status.get('num_in_osds', 0))
    full = status.get('full', False)
    nearfull = status.get('nearfull', False)

    if osds > up_osds:
        difference = osds - up_osds
        logger.warning('there %s %d OSD%s down' % (
            ['is', 'are'][difference != 1],
            difference,
            "s"[difference == 1:])
        )

    if osds > in_osds:
        difference = osds - in_osds
        logger.warning('there %s %d OSD%s out' % (
            ['is', 'are'][difference != 1],
            difference,
            "s"[difference == 1:])
        )

    if full:
        logger.warning('OSDs are full!')

    if nearfull:
        logger.warning('OSDs are near full!')


def prepare_disk(
        conn,
        cluster,
        data,
        journal,
        zap,
        fs_type,
        dmcrypt,
        dmcrypt_dir,
        storetype,
        block_wal,
        block_db,
        create=False):
    """
    Run on osd node, prepares a data disk for use.
    """
    ceph_volume_executable = system.executable_path(conn, 'ceph-volume')
    args = [
        ceph_volume_executable,
        '--cluster', cluster,
        'lvm',
        'create' if create else 'prepare',
        '--%s' % storetype,
        '--data', data
        ]
    if zap:
        logger.warning('zapping is no longer supported when preparing')
    if dmcrypt:
        args.append('--dmcrypt')
        # TODO: re-enable dmcrypt support once ceph-volume grows it
        logger.warning('dmcrypt is currently not supported')

    if storetype == 'bluestore':
        if block_wal:
            args.append('--block.wal')
            args.append(block_wal)
        if block_db:
            args.append('--block.db')
            args.append(block_db)
    elif storetype == 'filestore':
        if not journal:
            raise RuntimeError('A journal lv or GPT partition must be specified when using filestore')
        args.append('--journal')
        args.append(journal)

    remoto.process.run(
        conn,
        args
    )


def prepare(args, cfg, create=False):
    LOG.debug(
        'Preparing cluster %s on data device %s',
        args.cluster,
        args.data
        )

    key = get_bootstrap_osd_key(cluster=args.cluster)

    bootstrapped = set()
    errors = 0
    hostname = args.host

    try:
        if args.data is None:
            raise exc.NeedDiskError(hostname)

        distro = hosts.get(
            hostname,
            username=args.username,
            callbacks=[packages.ceph_is_installed]
        )
        LOG.info(
            'Distro info: %s %s %s',
            distro.name,
            distro.release,
            distro.codename
        )

        if hostname not in bootstrapped:
            bootstrapped.add(hostname)
            LOG.debug('Deploying osd to %s', hostname)

            conf_data = conf.ceph.load_raw(args)
            distro.conn.remote_module.write_conf(
                args.cluster,
                conf_data,
                args.overwrite_conf
            )

            create_osd_keyring(distro.conn, args.cluster, key)

        LOG.debug('Preparing host %s data %s',
                  hostname, args.data)# , journal, activate_prepared_disk)

        # default to bluestore unless explicitly told not to
        storetype = 'bluestore'
        if args.filestore:
            storetype = 'filestore'

        prepare_disk(
            distro.conn,
            cluster=args.cluster,
            data=args.data,
            journal=args.journal,
            zap=args.zap_disk,
            fs_type=args.fs_type,
            dmcrypt=args.dmcrypt,
            dmcrypt_dir=args.dmcrypt_key_dir,
            storetype=storetype,
            block_wal=args.block_wal,
            block_db=args.block_db,
            create=create,
        )

        # give the OSD a few seconds to start
        time.sleep(5)
        catch_osd_errors(distro.conn, distro.conn.logger, args)
        LOG.debug('Host %s is now ready for osd use.', hostname)
        distro.conn.exit()

    except RuntimeError as e:
        LOG.error(e)
        errors += 1

    if errors:
        raise exc.GenericError('Failed to create %d OSDs' % errors)


def activate(args, cfg):
    LOG.debug(
        'Activating cluster %s disks %s',
        args.cluster,
        # join elements of t with ':', t's with ' '
        # allow None in elements of t; print as empty
        ' '.join(':'.join((s or '') for s in t) for t in args.disk),
        )

    for hostname, disk, journal in args.disk:

        distro = hosts.get(
            hostname,
            username=args.username,
            callbacks=[packages.ceph_is_installed]
        )
        LOG.info(
            'Distro info: %s %s %s',
            distro.name,
            distro.release,
            distro.codename
        )

        LOG.debug('activating host %s disk %s', hostname, disk)
        LOG.debug('will use init type: %s', distro.init)

        ceph_disk_executable = system.executable_path(distro.conn, 'ceph-disk')
        remoto.process.run(
            distro.conn,
            [
                ceph_disk_executable,
                '-v',
                'activate',
                '--mark-init',
                distro.init,
                '--mount',
                disk,
            ],
        )
        # give the OSD a few seconds to start
        time.sleep(5)
        catch_osd_errors(distro.conn, distro.conn.logger, args)

        if distro.init == 'systemd':
            system.enable_service(distro.conn, "ceph.target")
        elif distro.init == 'sysvinit':
            system.enable_service(distro.conn, "ceph")

        distro.conn.exit()


def disk_zap(args):

    for hostname, disk, journal in args.disk:
        if not disk or not hostname:
            raise RuntimeError('zap command needs both HOSTNAME and DISK but got "%s %s"' % (hostname, disk))
        LOG.debug('zapping %s on %s', disk, hostname)
        distro = hosts.get(
            hostname,
            username=args.username,
            callbacks=[packages.ceph_is_installed]
        )
        LOG.info(
            'Distro info: %s %s %s',
            distro.name,
            distro.release,
            distro.codename
        )

        distro.conn.remote_module.zeroing(disk)

        ceph_disk_executable = system.executable_path(distro.conn, 'ceph-disk')
        remoto.process.run(
            distro.conn,
            [
                ceph_disk_executable,
                'zap',
                disk,
            ],
        )

        distro.conn.exit()


def disk_list(args, cfg):
    for hostname, disk, journal in args.disk:
        distro = hosts.get(
            hostname,
            username=args.username,
            callbacks=[packages.ceph_is_installed]
        )
        LOG.info(
            'Distro info: %s %s %s',
            distro.name,
            distro.release,
            distro.codename
        )

        LOG.debug('Listing disks on {hostname}...'.format(hostname=hostname))
        ceph_disk_executable = system.executable_path(distro.conn, 'ceph-disk')
        remoto.process.run(
            distro.conn,
            [
                ceph_disk_executable,
                'list',
            ],
        )
        distro.conn.exit()


def osd_list(args, cfg):
    monitors = mon.get_mon_initial_members(args, error_on_empty=True, _cfg=cfg)

    # get the osd tree from a monitor host
    mon_host = monitors[0]
    distro = hosts.get(
        mon_host,
        username=args.username,
        callbacks=[packages.ceph_is_installed]
    )

    tree = osd_tree(distro.conn, args.cluster)
    distro.conn.exit()

    interesting_files = ['active', 'magic', 'whoami', 'journal_uuid']

    for hostname, disk, journal in args.disk:
        distro = hosts.get(hostname, username=args.username)
        remote_module = distro.conn.remote_module
        osds = distro.conn.remote_module.listdir(constants.osd_path)

        ceph_disk_executable = system.executable_path(distro.conn, 'ceph-disk')
        output, err, exit_code = remoto.process.check(
            distro.conn,
            [
                ceph_disk_executable,
                'list',
            ]
        )

        for _osd in osds:
            osd_path = os.path.join(constants.osd_path, _osd)
            journal_path = os.path.join(osd_path, 'journal')
            _id = int(_osd.split('-')[-1])  # split on dash, get the id
            osd_name = 'osd.%s' % _id
            metadata = {}
            json_blob = {}

            # piggy back from ceph-disk and get the mount point
            device = get_osd_mount_point(output, osd_name)
            if device:
                metadata['device'] = device

            # read interesting metadata from files
            for f in interesting_files:
                osd_f_path = os.path.join(osd_path, f)
                if remote_module.path_exists(osd_f_path):
                    metadata[f] = remote_module.readline(osd_f_path)

            # do we have a journal path?
            if remote_module.path_exists(journal_path):
                metadata['journal path'] = remote_module.get_realpath(journal_path)

            # is this OSD in osd tree?
            for blob in tree['nodes']:
                if blob.get('id') == _id:  # matches our OSD
                    json_blob = blob

            print_osd(
                distro.conn.logger,
                hostname,
                osd_path,
                json_blob,
                metadata,
            )

        distro.conn.exit()


def get_osd_mount_point(output, osd_name):
    """
    piggy back from `ceph-disk list` output and get the mount point
    by matching the line where the partition mentions the OSD name

    For example, if the name of the osd is `osd.1` and the output from
    `ceph-disk list` looks like this::

        /dev/sda :
         /dev/sda1 other, ext2, mounted on /boot
         /dev/sda2 other
         /dev/sda5 other, LVM2_member
        /dev/sdb :
         /dev/sdb1 ceph data, active, cluster ceph, osd.1, journal /dev/sdb2
         /dev/sdb2 ceph journal, for /dev/sdb1
        /dev/sr0 other, unknown
        /dev/sr1 other, unknown

    Then `/dev/sdb1` would be the right mount point. We piggy back like this
    because ceph-disk does *a lot* to properly calculate those values and we
    don't want to re-implement all the helpers for this.

    :param output: A list of lines from stdout
    :param osd_name: The actual osd name, like `osd.1`
    """
    for line in output:
        line_parts = re.split(r'[,\s]+', line)
        for part in line_parts:
            mount_point = line_parts[1]
            if osd_name == part:
                return mount_point


def print_osd(logger, hostname, osd_path, json_blob, metadata, journal=None):
    """
    A helper to print OSD metadata
    """
    logger.info('-'*40)
    logger.info('%s' % osd_path.split('/')[-1])
    logger.info('-'*40)
    logger.info('%-14s %s' % ('Path', osd_path))
    logger.info('%-14s %s' % ('ID', json_blob.get('id')))
    logger.info('%-14s %s' % ('Name', json_blob.get('name')))
    logger.info('%-14s %s' % ('Status', json_blob.get('status')))
    logger.info('%-14s %s' % ('Reweight', json_blob.get('reweight')))
    if journal:
        logger.info('Journal: %s' % journal)
    for k, v in metadata.items():
        logger.info("%-13s  %s" % (k.capitalize(), v))

    logger.info('-'*40)


def osd(args):
    cfg = conf.ceph.load(args)

    if args.subcommand == 'list':
        osd_list(args, cfg)
    elif args.subcommand == 'prepare':
        prepare(args, cfg, create=False)
    elif args.subcommand == 'create':
        prepare(args, cfg, create=True)
    elif args.subcommand == 'activate':
        activate(args, cfg)
    else:
        LOG.error('subcommand %s not implemented', args.subcommand)
        sys.exit(1)


def disk(args):
    cfg = conf.ceph.load(args)

    if args.subcommand == 'list':
        disk_list(args, cfg)
    elif args.subcommand == 'prepare':
        prepare(args, cfg, create=False)
    elif args.subcommand == 'create':
        prepare(args, cfg, create=True)
    elif args.subcommand == 'activate':
        activate(args, cfg)
    elif args.subcommand == 'zap':
        disk_zap(args)
    else:
        LOG.error('subcommand %s not implemented', args.subcommand)
        sys.exit(1)


@priority(50)
def make(parser):
    """
    Prepare a data disk on remote host.
    """
    sub_command_help = dedent("""
    Manage OSDs by preparing a data disk on remote host.

    For paths, first prepare and then activate:

        ceph-deploy osd prepare {osd-node-name}:/path/to/osd
        ceph-deploy osd activate {osd-node-name}:/path/to/osd

    For disks or journals the `create` command will do prepare and activate
    for you.
    """
    )
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.description = sub_command_help

    osd_parser = parser.add_subparsers(dest='subcommand')
    osd_parser.required = True

    osd_list = osd_parser.add_parser(
        'list',
        help='List OSD info from remote host(s)'
        )
    osd_list.add_argument(
        'host',
        nargs='?',
        metavar='HOST',
        help='remote host to list OSDs from'
        )

    osd_create = osd_parser.add_parser(
        'create',
        help='Create new Ceph OSD daemon by preparing and activating a device'
    )
    osd_create.add_argument(
        '--data',
        metavar='DATA',
        help='The OSD data logical volume (vg/lv) or device'
    )
    osd_create.add_argument(
        '--journal',
        help='Logical Volume (vg/lv) or path to GPT partition',
        )
    osd_create.add_argument(
        '--zap-disk',
        action='store_true',
        help='DEPRECATED - cannot zap when creating an OSD'
    )
    osd_create.add_argument(
        '--fs-type',
        metavar='FS_TYPE',
        choices=['xfs',
                 'btrfs'
                 ],
        default='xfs',
        help='filesystem to use to format DEVICE (xfs, btrfs)',
        )
    osd_create.add_argument(
        '--dmcrypt',
        action='store_true',
        help='use dm-crypt on DEVICE',
        )
    osd_create.add_argument(
        '--dmcrypt-key-dir',
        metavar='KEYDIR',
        default='/etc/ceph/dmcrypt-keys',
        help='directory where dm-crypt keys are stored',
        )
    osd_create.add_argument(
        '--filestore',
        action='store_true', default=None,
        help='filestore objectstore',
        )
    osd_create.add_argument(
        '--bluestore',
        action='store_true', default=None,
        help='bluestore objectstore',
        )
    osd_create.add_argument(
        '--block-db',
        default=None,
        help='bluestore block.db path'
        )
    osd_create.add_argument(
        '--block-wal',
        default=None,
        help='bluestore block.wal path'
        )
    osd_create.add_argument(
        'host',
        nargs='?',
        metavar='HOST',
        help='Remote host to connect'
        )
    osd_prepare = osd_parser.add_parser(
        'prepare',
        help='Prepare an LV or device for use as Ceph OSD'
        )
    osd_prepare.add_argument(
        '--filestore',
        action='store_true', default=None,
        help='filestore objectstore',
        )
    osd_prepare.add_argument(
        '--zap-disk',
        action='store_true',
        help='destroy existing content for DEVICE',
        )
    osd_prepare.add_argument(
        '--fs-type',
        metavar='FS_TYPE',
        choices=['xfs',
                 'btrfs'
                 ],
        default='xfs',
        help='filesystem to use to format DEVICE (xfs, btrfs)',
        )
    osd_prepare.add_argument(
        '--dmcrypt',
        action='store_true',
        help='use dm-crypt on DEVICE',
        )
    osd_prepare.add_argument(
        '--dmcrypt-key-dir',
        metavar='KEYDIR',
        default='/etc/ceph/dmcrypt-keys',
        help='directory where dm-crypt keys are stored',
        )
    osd_prepare.add_argument(
        '--bluestore',
        action='store_true', default=None,
        help='bluestore objectstore',
        )
    osd_prepare.add_argument(
        '--block-db',
        default=None,
        help='bluestore block.db path'
        )
    osd_prepare.add_argument(
        '--block-wal',
        default=None,
        help='bluestore block.wal path'
        )
    osd_prepare.add_argument(
        '--journal',
        help='Logical Volume (vg/lv) or path to GPT partition',
        )
    osd_prepare.add_argument(
        '--data',
        metavar='DATA',
        help='Logical Volume (vg/lv) or path to device',
        )
    osd_prepare.add_argument(
        'host',
        nargs='?',
        metavar='HOST',
        help='Remote host to connect'
        )
    osd_activate = osd_parser.add_parser(
        'activate',
        help='Start (activate) Ceph OSD that was previously prepared'
        )
    osd_activate.add_argument(
        '--osd-fsid',
        metavar='FSID',
        help='The FSID of the previously prepared OSD'
        )
    osd_activate.add_argument(
        '--osd-id',
        metavar='ID',
        help='The ID of the previously prepared OSD'
        )
    osd_activate.add_argument(
        'host',
        nargs='?',
        metavar='HOST',
        help='Remote host to connect'
        )
    parser.set_defaults(
        func=osd,
        )


@priority(50)
def make_disk(parser):
    """
    Manage disks on a remote host.
    """
    disk_parser = parser.add_subparsers(dest='subcommand')
    disk_parser.required = True

    disk_zap = disk_parser.add_parser(
        'zap',
        help='destroy existing data and filesystem on LV or partition',
        )
    disk_zap.add_argument(
        'host',
        nargs='?',
        metavar='HOST',
        help='Remote host to connect'
        )

    disk_list = disk_parser.add_parser(
        'list',
        help='List disk info from remote host(s)'
        )
    disk_list.add_argument(
        'host',
        metavar='HOST',
        help='Remote host to list OSDs from'
        )

    disk_prepare = disk_parser.add_parser(
        'prepare',
        help='Prepare a disk for use as Ceph OSD'
        )
    disk_prepare.add_argument(
        '--zap-disk',
        action='store_true',
        help='DEPRECATED - no longer zaps before preparing',
        )
    disk_prepare.add_argument(
        '--fs-type',
        metavar='FS_TYPE',
        choices=['xfs',
                 'btrfs'
                 ],
        default='xfs',
        help='filesystem to use to format DEVICE (xfs, btrfs)',
        )
    disk_prepare.add_argument(
        '--dmcrypt',
        action='store_true',
        help='use dm-crypt on DEVICE',
        )
    disk_prepare.add_argument(
        '--dmcrypt-key-dir',
        metavar='KEYDIR',
        default='/etc/ceph/dmcrypt-keys',
        help='directory where dm-crypt keys are stored',
        )
    disk_prepare.add_argument(
        '--bluestore',
        action='store_true', default=None,
        help='bluestore objectstore',
        )
    disk_prepare.add_argument(
        '--filestore',
        action='store_true', default=None,
        help='filestore objectstore',
        )
    disk_prepare.add_argument(
        '--block-db',
        default=None,
        help='bluestore block.db path'
        )
    disk_prepare.add_argument(
        '--block-wal',
        default=None,
        help='bluestore block.wal path'
        )
    disk_prepare.add_argument(
        'host',
        nargs='?',
        metavar='HOST',
        help='Remote host to connect'
        )

    disk_activate = disk_parser.add_parser(
        'activate',
        help='Start (activate) Ceph OSD from disk that was previously prepared'
        )
    disk_activate.add_argument(
        'host',
        nargs='?',
        metavar='HOST',
        help='Remote host to connect'
        )
    parser.set_defaults(
        func=disk,
        )
