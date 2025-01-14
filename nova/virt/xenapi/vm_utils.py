# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2010 Citrix Systems, Inc.
# Copyright 2011 Piston Cloud Computing, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Helper methods for operations related to the management of VM records and
their attributes like VDIs, VIFs, as well as their lookup functions.
"""

import contextlib
import json
import os
import pickle
import re
import sys
import tempfile
import time
import urllib
import uuid
from decimal import Decimal
from xml.dom import minidom

from nova import exception
from nova import flags
from nova.image import glance
from nova import log as logging
from nova import utils
from nova.compute import instance_types
from nova.compute import power_state
from nova.virt.disk import api as disk
from nova.virt.xenapi import HelperBase
from nova.virt.xenapi import volume_utils


LOG = logging.getLogger("nova.virt.xenapi.vm_utils")

FLAGS = flags.FLAGS
flags.DEFINE_string('default_os_type', 'linux', 'Default OS type')
flags.DEFINE_integer('block_device_creation_timeout', 10,
                     'time to wait for a block device to be created')
flags.DEFINE_integer('max_kernel_ramdisk_size', 16 * 1024 * 1024,
                     'maximum size in bytes of kernel or ramdisk images')

XENAPI_POWER_STATE = {
    'Halted': power_state.SHUTDOWN,
    'Running': power_state.RUNNING,
    'Paused': power_state.PAUSED,
    'Suspended': power_state.SUSPENDED,
    'Crashed': power_state.CRASHED}


SECTOR_SIZE = 512
MBR_SIZE_SECTORS = 63
MBR_SIZE_BYTES = MBR_SIZE_SECTORS * SECTOR_SIZE
KERNEL_DIR = '/boot/guest'


class ImageType:
    """
    Enumeration class for distinguishing different image types
        0 - kernel image (goes on dom0's filesystem)
        1 - ramdisk image (goes on dom0's filesystem)
        2 - disk image (local SR, partitioned by objectstore plugin)
        3 - raw disk image (local SR, NOT partitioned by plugin)
        4 - vhd disk image (local SR, NOT inspected by XS, PV assumed for
            linux, HVM assumed for Windows)
        5 - ISO disk image (local SR, NOT partitioned by plugin)
    """

    KERNEL = 0
    RAMDISK = 1
    DISK = 2
    DISK_RAW = 3
    DISK_VHD = 4
    DISK_ISO = 5
    _ids = (KERNEL, RAMDISK, DISK, DISK_RAW, DISK_VHD, DISK_ISO)

    KERNEL_STR = "kernel"
    RAMDISK_STR = "ramdisk"
    DISK_STR = "os"
    DISK_RAW_STR = "os_raw"
    DISK_VHD_STR = "vhd"
    DISK_ISO_STR = "iso"
    _strs = (KERNEL_STR, RAMDISK_STR, DISK_STR, DISK_RAW_STR, DISK_VHD_STR,
                DISK_ISO_STR)

    @classmethod
    def to_string(cls, image_type):
        return dict(zip(ImageType._ids, ImageType._strs)).get(image_type)

    @classmethod
    def from_string(cls, image_type_str):
        return dict(zip(ImageType._strs, ImageType._ids)).get(image_type_str)


class VMHelper(HelperBase):
    """
    The class that wraps the helper methods together.
    """

    @classmethod
    def create_vm(cls, session, instance, kernel, ramdisk,
                  use_pv_kernel=False):
        """Create a VM record.  Returns a Deferred that gives the new
        VM reference.
        the use_pv_kernel flag indicates whether the guest is HVM or PV

        There are 3 scenarios:

            1. Using paravirtualization,  kernel passed in

            2. Using paravirtualization, kernel within the image

            3. Using hardware virtualization
        """
        inst_type_id = instance.instance_type_id
        instance_type = instance_types.get_instance_type(inst_type_id)
        mem = str(long(instance_type['memory_mb']) * 1024 * 1024)
        vcpus = str(instance_type['vcpus'])

        rec = {
            'actions_after_crash': 'destroy',
            'actions_after_reboot': 'restart',
            'actions_after_shutdown': 'destroy',
            'affinity': '',
            'blocked_operations': {},
            'ha_always_run': False,
            'ha_restart_priority': '',
            'HVM_boot_params': {},
            'HVM_boot_policy': '',
            'is_a_template': False,
            'memory_dynamic_min': mem,
            'memory_dynamic_max': mem,
            'memory_static_min': '0',
            'memory_static_max': mem,
            'memory_target': mem,
            'name_description': '',
            'name_label': instance.name,
            'other_config': {'allowvssprovider': False},
            'other_config': {},
            'PCI_bus': '',
            'platform': {'acpi': 'true', 'apic': 'true', 'pae': 'true',
                         'viridian': 'true', 'timeoffset': '0'},
            'PV_args': '',
            'PV_bootloader': '',
            'PV_bootloader_args': '',
            'PV_kernel': '',
            'PV_legacy_args': '',
            'PV_ramdisk': '',
            'recommendations': '',
            'tags': [],
            'user_version': '0',
            'VCPUs_at_startup': vcpus,
            'VCPUs_max': vcpus,
            'VCPUs_params': {},
            'xenstore_data': {}}

        # Complete VM configuration record according to the image type
        # non-raw/raw with PV kernel/raw in HVM mode
        if use_pv_kernel:
            rec['platform']['nx'] = 'false'
            if instance.kernel_id:
                # 1. Kernel explicitly passed in, use that
                rec['PV_args'] = 'root=/dev/xvda1'
                rec['PV_kernel'] = kernel
                rec['PV_ramdisk'] = ramdisk
            else:
                # 2. Use kernel within the image
                rec['PV_bootloader'] = 'pygrub'
        else:
            # 3. Using hardware virtualization
            rec['platform']['nx'] = 'true'
            rec['HVM_boot_params'] = {'order': 'dc'}
            rec['HVM_boot_policy'] = 'BIOS order'

        LOG.debug(_('Created VM %s...'), instance.name)
        vm_ref = session.call_xenapi('VM.create', rec)
        instance_name = instance.name
        LOG.debug(_('Created VM %(instance_name)s as %(vm_ref)s.') % locals())
        return vm_ref

    @classmethod
    def ensure_free_mem(cls, session, instance):
        inst_type_id = instance.instance_type_id
        instance_type = instance_types.get_instance_type(inst_type_id)
        mem = long(instance_type['memory_mb']) * 1024 * 1024
        #get free memory from host
        host = session.get_xenapi_host()
        host_free_mem = long(session.call_xenapi("host.compute_free_memory",
                                                 host))
        return host_free_mem >= mem

    @classmethod
    def create_cd_vbd(cls, session, vm_ref, vdi_ref, userdevice, bootable):
        """Create a VBD record.  Returns a Deferred that gives the new
        VBD reference specific to CDRom devices."""
        vbd_rec = {}
        vbd_rec['VM'] = vm_ref
        vbd_rec['VDI'] = vdi_ref
        vbd_rec['userdevice'] = str(userdevice)
        vbd_rec['bootable'] = bootable
        vbd_rec['mode'] = 'RO'
        vbd_rec['type'] = 'CD'
        vbd_rec['unpluggable'] = True
        vbd_rec['empty'] = False
        vbd_rec['other_config'] = {}
        vbd_rec['qos_algorithm_type'] = ''
        vbd_rec['qos_algorithm_params'] = {}
        vbd_rec['qos_supported_algorithms'] = []
        LOG.debug(_('Creating a CDROM-specific VBD for VM %(vm_ref)s,'
                ' VDI %(vdi_ref)s ... ') % locals())
        vbd_ref = session.call_xenapi('VBD.create', vbd_rec)
        LOG.debug(_('Created a CDROM-specific VBD %(vbd_ref)s '
                ' for VM %(vm_ref)s, VDI %(vdi_ref)s.') % locals())
        return vbd_ref

    @classmethod
    def find_vbd_by_number(cls, session, vm_ref, number):
        """Get the VBD reference from the device number"""
        vbd_refs = session.call_xenapi("VM.get_VBDs", vm_ref)
        if vbd_refs:
            for vbd_ref in vbd_refs:
                try:
                    vbd_rec = session.call_xenapi("VBD.get_record", vbd_ref)
                    if vbd_rec['userdevice'] == str(number):
                        return vbd_ref
                except cls.XenAPI.Failure, exc:
                    LOG.exception(exc)
        raise volume_utils.StorageError(
                _('VBD not found in instance %s') % vm_ref)

    @classmethod
    def unplug_vbd(cls, session, vbd_ref):
        """Unplug VBD from VM"""
        try:
            vbd_ref = session.call_xenapi('VBD.unplug', vbd_ref)
        except cls.XenAPI.Failure, exc:
            LOG.exception(exc)
            if exc.details[0] != 'DEVICE_ALREADY_DETACHED':
                raise volume_utils.StorageError(
                        _('Unable to unplug VBD %s') % vbd_ref)

    @classmethod
    def destroy_vbd(cls, session, vbd_ref):
        """Destroy VBD from host database"""
        try:
            task = session.call_xenapi('Async.VBD.destroy', vbd_ref)
            session.wait_for_task(task)
        except cls.XenAPI.Failure, exc:
            LOG.exception(exc)
            raise volume_utils.StorageError(
                    _('Unable to destroy VBD %s') % vbd_ref)

    @classmethod
    def destroy_vdi(cls, session, vdi_ref):
        try:
            task = session.call_xenapi('Async.VDI.destroy', vdi_ref)
            session.wait_for_task(task)
        except cls.XenAPI.Failure, exc:
            LOG.exception(exc)
            raise volume_utils.StorageError(
                    _('Unable to destroy VDI %s') % vdi_ref)

    @classmethod
    def create_vdi(cls, session, sr_ref, name_label, virtual_size, read_only):
        """Create a VDI record and returns its reference."""
        vdi_ref = session.call_xenapi("VDI.create",
             {'name_label': name_label,
              'name_description': '',
              'SR': sr_ref,
              'virtual_size': str(virtual_size),
              'type': 'User',
              'sharable': False,
              'read_only': read_only,
              'xenstore_data': {},
              'other_config': {},
              'sm_config': {},
              'tags': []})
        LOG.debug(_('Created VDI %(vdi_ref)s (%(name_label)s,'
                ' %(virtual_size)s, %(read_only)s) on %(sr_ref)s.')
                % locals())
        return vdi_ref

    @classmethod
    def set_vdi_name_label(cls, session, vdi_uuid, name_label):
        vdi_ref = session.call_xenapi("VDI.get_by_uuid", vdi_uuid)
        session.call_xenapi("VDI.set_name_label", vdi_ref, name_label)

    @classmethod
    def get_vdi_for_vm_safely(cls, session, vm_ref):
        """Retrieves the primary VDI for a VM"""
        vbd_refs = session.call_xenapi("VM.get_VBDs", vm_ref)
        for vbd in vbd_refs:
            vbd_rec = session.call_xenapi("VBD.get_record", vbd)
            # Convention dictates the primary VDI will be userdevice 0
            if vbd_rec['userdevice'] == '0':
                vdi_rec = session.call_xenapi("VDI.get_record", vbd_rec['VDI'])
                return vbd_rec['VDI'], vdi_rec
        raise exception.Error(_("No primary VDI found for"
                "%(vm_ref)s") % locals())

    @classmethod
    def create_snapshot(cls, session, instance, vm_ref, label):
        """Creates Snapshot (Template) VM, Snapshot VBD, Snapshot VDI,
        Snapshot VHD"""
        LOG.debug(_("Snapshotting VM %(vm_ref)s with label '%(label)s'...")
                % locals())

        vm_vdi_ref, vm_vdi_rec = cls.get_vdi_for_vm_safely(session, vm_ref)
        sr_ref = vm_vdi_rec["SR"]

        original_parent_uuid = get_vhd_parent_uuid(session, vm_vdi_ref)

        task = session.call_xenapi('Async.VM.snapshot', vm_ref, label)
        template_vm_ref = session.wait_for_task(task, instance['uuid'])
        template_vdi_rec = cls.get_vdi_for_vm_safely(session,
                template_vm_ref)[1]
        template_vdi_uuid = template_vdi_rec["uuid"]

        LOG.debug(_('Created snapshot %(template_vm_ref)s from'
                ' VM %(vm_ref)s.') % locals())

        parent_uuid = _wait_for_vhd_coalesce(
            session, instance, sr_ref, vm_vdi_ref, original_parent_uuid)

        #TODO(sirp): we need to assert only one parent, not parents two deep
        template_vdi_uuids = {'image': parent_uuid,
                              'snap': template_vdi_uuid}
        return template_vm_ref, template_vdi_uuids

    @classmethod
    def get_sr_path(cls, session):
        """Return the path to our storage repository

        This is used when we're dealing with VHDs directly, either by taking
        snapshots or by restoring an image in the DISK_VHD format.
        """
        sr_ref = cls.safe_find_sr(session)
        sr_rec = session.call_xenapi("SR.get_record", sr_ref)
        sr_uuid = sr_rec["uuid"]
        return os.path.join(FLAGS.xenapi_sr_base_path, sr_uuid)

    @classmethod
    def upload_image(cls, context, session, instance, vdi_uuids, image_id):
        """ Requests that the Glance plugin bundle the specified VDIs and
        push them into Glance using the specified human-friendly name.
        """
        # NOTE(sirp): Currently we only support uploading images as VHD, there
        # is no RAW equivalent (yet)
        logging.debug(_("Asking xapi to upload %(vdi_uuids)s as"
                " ID %(image_id)s") % locals())

        glance_host, glance_port = glance.pick_glance_api_server()

        properties = {}
        properties['auto_disk_config'] = instance.auto_disk_config
        properties['os_type'] = instance.os_type or FLAGS.default_os_type

        params = {'vdi_uuids': vdi_uuids,
                  'image_id': image_id,
                  'glance_host': glance_host,
                  'glance_port': glance_port,
                  'sr_path': cls.get_sr_path(session),
                  'auth_token': getattr(context, 'auth_token', None),
                  'properties': properties}

        kwargs = {'params': pickle.dumps(params)}
        task = session.async_call_plugin('glance', 'upload_vhd', kwargs)
        session.wait_for_task(task, instance['uuid'])

    @classmethod
    def resize_disk(cls, session, vdi_ref, instance_type):
        # Copy VDI over to something we can resize
        # NOTE(jerdfelt): Would be nice to just set vdi_ref to read/write
        sr_ref = cls.safe_find_sr(session)
        copy_ref = session.call_xenapi('VDI.copy', vdi_ref, sr_ref)
        copy_uuid = session.call_xenapi('VDI.get_uuid', copy_ref)

        try:
            # Resize partition and filesystem down
            cls.auto_configure_disk(session=session,
                                    vdi_ref=copy_ref,
                                    new_gb=instance_type['local_gb'])

            # Create new VDI
            new_ref = cls.fetch_blank_disk(session,
                                           instance_type['id'])
            new_uuid = session.call_xenapi('VDI.get_uuid', new_ref)

            # Manually copy contents over
            virtual_size = instance_type['local_gb'] * 1024 * 1024 * 1024
            _copy_partition(session, copy_ref, new_ref, 1, virtual_size)

            return new_ref, new_uuid
        finally:
            cls.destroy_vdi(session, copy_ref)

    @classmethod
    def auto_configure_disk(cls, session, vdi_ref, new_gb):
        """Partition and resize FS to match the size specified by
        instance_types.local_gb.

        This is a fail-safe to prevent accidentally destroying data on a disk
        erroneously marked as auto_disk_config=True.

        The criteria for allowing resize are:

            1. 'auto_disk_config' must be true for the instance (and image).
               (If we've made it here, then auto_disk_config=True.)

            2. The disk must have only one partition.

            3. The file-system on the one partition must be ext3 or ext4.
        """
        with vdi_attached_here(session, vdi_ref, read_only=False) as dev:
            partitions = _get_partitions(dev)

            if len(partitions) != 1:
                return

            num, start, old_sectors, ptype = partitions[0]
            if ptype in ('ext3', 'ext4'):
                new_sectors = new_gb * 1024 * 1024 * 1024 / SECTOR_SIZE
                _resize_part_and_fs(dev, start, old_sectors, new_sectors)

    @classmethod
    def generate_swap(cls, session, instance, vm_ref, userdevice, swap_mb):
        """
        Steps to programmatically generate swap:

            1. Create VDI of desired swap size

            2. Attach VDI to compute worker

            3. Create swap partition

            4. Create VBD between instance VM and swap VDI
        """
        # 1. Create VDI
        sr_ref = cls.safe_find_sr(session)
        name_label = instance.name + "-swap"
        ONE_MEG = 1024 * 1024
        virtual_size = swap_mb * ONE_MEG
        vdi_ref = cls.create_vdi(
            session, sr_ref, name_label, virtual_size, read_only=False)

        try:
            # 2. Attach VDI to compute worker (VBD hotplug)
            with vdi_attached_here(session, vdi_ref, read_only=False) as dev:
                # 3. Create swap partition

                # NOTE(jk0): We use a FAT32 filesystem for the Windows swap
                # partition because that is what parted supports.
                is_windows = instance.os_type == "windows"
                fs_type = "fat32" if is_windows else "linux-swap"

                dev_path = utils.make_dev_path(dev)
                utils.execute('parted', '--script', dev_path,
                              'mklabel', 'msdos', run_as_root=True)

                partition_start = 0
                partition_end = swap_mb
                utils.execute('parted', '--script', dev_path, 'mkpartfs',
                              'primary', fs_type,
                              str(partition_start),
                              str(partition_end),
                              run_as_root=True)

            # 4. Create VBD between instance VM and swap VDI
            volume_utils.VolumeHelper.create_vbd(
                session, vm_ref, vdi_ref, userdevice, bootable=False)
        except:
            with utils.save_and_reraise_exception():
                cls.destroy_vdi(session, vdi_ref)

    @classmethod
    def fetch_blank_disk(cls, session, instance_type_id):
        # Size the blank harddrive to suit the machine type:
        one_gig = 1024 * 1024 * 1024
        req_type = instance_types.get_instance_type(instance_type_id)
        req_size = req_type['local_gb']

        LOG.debug("Creating blank HD of size %(req_size)d gigs"
                    % locals())
        vdi_size = one_gig * req_size

        LOG.debug("ISO vm create: Looking for the SR")
        sr_ref = cls.safe_find_sr(session)

        vdi_ref = cls.create_vdi(session, sr_ref, 'blank HD', vdi_size, False)
        return vdi_ref

    @classmethod
    def fetch_image(cls, context, session, instance, image, user_id,
                    project_id, image_type):
        """Fetch image from glance based on image type.

        Returns: A single filename if image_type is KERNEL or RAMDISK
                 A list of dictionaries that describe VDIs, otherwise
        """
        if image_type == ImageType.DISK_VHD:
            return cls._fetch_image_glance_vhd(context,
                session, instance, image, image_type)
        else:
            return cls._fetch_image_glance_disk(context,
                session, instance, image, image_type)

    @classmethod
    def _fetch_image_glance_vhd(cls, context, session, instance, image,
                                image_type):
        """Tell glance to download an image and put the VHDs into the SR

        Returns: A list of dictionaries that describe VDIs
        """
        instance_id = instance.id
        LOG.debug(_("Asking xapi to fetch vhd image %(image)s")
                    % locals())
        sr_ref = cls.safe_find_sr(session)

        # NOTE(sirp): The Glance plugin runs under Python 2.4
        # which does not have the `uuid` module. To work around this,
        # we generate the uuids here (under Python 2.6+) and
        # pass them as arguments
        uuid_stack = [str(uuid.uuid4()) for i in xrange(2)]

        glance_host, glance_port = glance.pick_glance_api_server()
        params = {'image_id': image,
                  'glance_host': glance_host,
                  'glance_port': glance_port,
                  'uuid_stack': uuid_stack,
                  'sr_path': cls.get_sr_path(session),
                  'num_retries': FLAGS.glance_num_retries,
                  'auth_token': getattr(context, 'auth_token', None)}

        kwargs = {'params': pickle.dumps(params)}
        task = session.async_call_plugin('glance', 'download_vhd', kwargs)
        result = session.wait_for_task(task, instance['uuid'])
        # 'download_vhd' will return a json encoded string containing
        # a list of dictionaries describing VDIs.  The dictionary will
        # contain 'vdi_type' and 'vdi_uuid' keys.  'vdi_type' can be
        # 'os' or 'swap' right now.
        vdis = json.loads(result)
        for vdi in vdis:
            LOG.debug(_("xapi 'download_vhd' returned VDI of "
                    "type '%(vdi_type)s' with UUID '%(vdi_uuid)s'" % vdi))

        cls.scan_sr(session, instance, sr_ref)

        # Pull out the UUID of the first VDI (which is the os VDI)
        os_vdi_uuid = vdis[0]['vdi_uuid']

        # Set the name-label to ease debugging
        vdi_ref = session.call_xenapi("VDI.get_by_uuid", os_vdi_uuid)
        primary_name_label = instance.name
        session.call_xenapi("VDI.set_name_label", vdi_ref, primary_name_label)

        cls._check_vdi_size(context, session, instance, os_vdi_uuid)
        return vdis

    @classmethod
    def _get_vdi_chain_size(cls, context, session, vdi_uuid):
        """Compute the total size of a VDI chain, starting with the specified
        VDI UUID.

        This will walk the VDI chain to the root, add the size of each VDI into
        the total.
        """
        size_bytes = 0
        for vdi_rec in walk_vdi_chain(session, vdi_uuid):
            cur_vdi_uuid = vdi_rec['uuid']
            vdi_size_bytes = int(vdi_rec['physical_utilisation'])
            LOG.debug(_('vdi_uuid=%(cur_vdi_uuid)s vdi_size_bytes='
                        '%(vdi_size_bytes)d' % locals()))
            size_bytes += vdi_size_bytes
        return size_bytes

    @classmethod
    def _check_vdi_size(cls, context, session, instance, vdi_uuid):
        size_bytes = cls._get_vdi_chain_size(context, session, vdi_uuid)

        # FIXME(jk0): this was copied directly from compute.manager.py, let's
        # refactor this to a common area
        instance_type_id = instance['instance_type_id']
        instance_type = instance_types.get_instance_type(instance_type_id)
        allowed_size_gb = instance_type['local_gb']
        allowed_size_bytes = allowed_size_gb * 1024 * 1024 * 1024

        LOG.debug(_("image_size_bytes=%(size_bytes)d, allowed_size_bytes="
                    "%(allowed_size_bytes)d") % locals())

        if size_bytes > allowed_size_bytes:
            LOG.info(_("Image size %(size_bytes)d exceeded"
                       " instance_type allowed size "
                       "%(allowed_size_bytes)d")
                       % locals())
            raise exception.ImageTooLarge()

    @classmethod
    def _fetch_image_glance_disk(cls, context, session, instance, image,
                                 image_type):
        """Fetch the image from Glance

        NOTE:
        Unlike _fetch_image_glance_vhd, this method does not use the Glance
        plugin; instead, it streams the disks through domU to the VDI
        directly.

        Returns: A single filename if image_type is KERNEL_RAMDISK
                 A list of dictionaries that describe VDIs, otherwise
        """
        instance_id = instance.id
        # FIXME(sirp): Since the Glance plugin seems to be required for the
        # VHD disk, it may be worth using the plugin for both VHD and RAW and
        # DISK restores
        LOG.debug(_("Fetching image %(image)s") % locals())
        LOG.debug(_("Image Type: %s"), ImageType.to_string(image_type))

        if image_type == ImageType.DISK_ISO:
            sr_ref = cls.safe_find_iso_sr(session)
            LOG.debug(_("ISO: Found sr possibly containing the ISO image"))
        else:
            sr_ref = cls.safe_find_sr(session)

        glance_client, image_id = glance.get_glance_client(context, image)
        glance_client.set_auth_token(getattr(context, 'auth_token', None))
        meta, image_file = glance_client.get_image(image_id)
        virtual_size = int(meta['size'])
        vdi_size = virtual_size
        LOG.debug(_("Size for image %(image)s:" +
                    "%(virtual_size)d") % locals())
        if image_type == ImageType.DISK:
            # Make room for MBR.
            vdi_size += MBR_SIZE_BYTES
        elif image_type in (ImageType.KERNEL, ImageType.RAMDISK) and \
             vdi_size > FLAGS.max_kernel_ramdisk_size:
            max_size = FLAGS.max_kernel_ramdisk_size
            raise exception.Error(
                _("Kernel/Ramdisk image is too large: %(vdi_size)d bytes, "
                  "max %(max_size)d bytes") % locals())

        name_label = instance.name
        vdi_ref = cls.create_vdi(session, sr_ref, name_label, vdi_size, False)
        # From this point we have a VDI on Xen host;
        # If anything goes wrong, we need to remember its uuid.
        try:
            filename = None
            vdi_uuid = session.call_xenapi("VDI.get_uuid", vdi_ref)

            with vdi_attached_here(session, vdi_ref, read_only=False) as dev:
                _stream_disk(dev, image_type, virtual_size, image_file)

            if image_type in (ImageType.KERNEL, ImageType.RAMDISK):
                # We need to invoke a plugin for copying the
                # content of the VDI into the proper path.
                LOG.debug(_("Copying VDI %s to /boot/guest on dom0"), vdi_ref)
                fn = "copy_kernel_vdi"
                args = {}
                args['vdi-ref'] = vdi_ref
                # Let the plugin copy the correct number of bytes.
                args['image-size'] = str(vdi_size)
                task = session.async_call_plugin('glance', fn, args)
                filename = session.wait_for_task(task, instance['uuid'])
                # Remove the VDI as it is not needed anymore.
                session.call_xenapi("VDI.destroy", vdi_ref)
                LOG.debug(_("Kernel/Ramdisk VDI %s destroyed"), vdi_ref)
                return [dict(vdi_type=ImageType.to_string(image_type),
                             vdi_uuid=None,
                             file=filename)]
            else:
                return [dict(vdi_type=ImageType.to_string(image_type),
                             vdi_uuid=vdi_uuid,
                             file=None)]
        except (cls.XenAPI.Failure, IOError, OSError) as e:
            # We look for XenAPI and OS failures.
            LOG.exception(_("instance %s: Failed to fetch glance image"),
                          instance_id, exc_info=sys.exc_info())
            e.args = e.args + ([dict(vdi_type=ImageType.
                                              to_string(image_type),
                                    vdi_uuid=vdi_uuid,
                                    file=filename)],)
            raise e

    @classmethod
    def determine_disk_image_type(cls, image_meta):
        """Disk Image Types are used to determine where the kernel will reside
        within an image. To figure out which type we're dealing with, we use
        the following rules:

        1. If we're using Glance, we can use the image_type field to
           determine the image_type

        2. If we're not using Glance, then we need to deduce this based on
           whether a kernel_id is specified.
        """
        def log_disk_format(image_type):
            pretty_format = {ImageType.KERNEL: 'KERNEL',
                             ImageType.RAMDISK: 'RAMDISK',
                             ImageType.DISK: 'DISK',
                             ImageType.DISK_RAW: 'DISK_RAW',
                             ImageType.DISK_VHD: 'DISK_VHD',
                             ImageType.DISK_ISO: 'DISK_ISO'}
            disk_format = pretty_format[image_type]
            image_ref = image_meta['id']
            LOG.debug(_("Detected %(disk_format)s format for image "
                        "%(image_ref)s") % locals())

        def determine_from_image_meta():
            glance_disk_format2nova_type = {
                'ami': ImageType.DISK,
                'aki': ImageType.KERNEL,
                'ari': ImageType.RAMDISK,
                'raw': ImageType.DISK_RAW,
                'vhd': ImageType.DISK_VHD,
                'iso': ImageType.DISK_ISO}
            disk_format = image_meta['disk_format']
            try:
                return glance_disk_format2nova_type[disk_format]
            except KeyError:
                raise exception.InvalidDiskFormat(disk_format=disk_format)

        image_type = determine_from_image_meta()

        log_disk_format(image_type)
        return image_type

    @classmethod
    def determine_is_pv(cls, session, instance_id, vdi_ref, disk_image_type,
                        os_type):
        """
        Determine whether the VM will use a paravirtualized kernel or if it
        will use hardware virtualization.

            1. Glance (VHD): then we use `os_type`, raise if not set

            2. Glance (DISK_RAW): use Pygrub to figure out if pv kernel is
               available

            3. Glance (DISK): pv is assumed

            4. Glance (DISK_ISO): no pv is assumed
        """

        LOG.debug(_("Looking up vdi %s for PV kernel"), vdi_ref)
        if disk_image_type == ImageType.DISK_VHD:
            # 1. VHD
            if os_type == 'windows':
                is_pv = False
            else:
                is_pv = True
        elif disk_image_type == ImageType.DISK_RAW:
            # 2. RAW
            with vdi_attached_here(session, vdi_ref, read_only=True) as dev:
                is_pv = _is_vdi_pv(dev)
        elif disk_image_type == ImageType.DISK:
            # 3. Disk
            is_pv = True
        elif disk_image_type == ImageType.DISK_ISO:
            # 4. ISO
            is_pv = False
        else:
            raise exception.Error(_("Unknown image format %(disk_image_type)s")
                                  % locals())

        return is_pv

    @classmethod
    def set_vm_name_label(cls, session, vm_ref, name_label):
        session.call_xenapi("VM.set_name_label", vm_ref, name_label)

    @classmethod
    def list_vms(cls, session):
        for vm_ref, vm_rec in cls.get_all_refs_and_recs(session, 'VM'):
            if vm_rec["is_a_template"] or vm_rec["is_control_domain"]:
                continue
            else:
                yield vm_ref, vm_rec

    @classmethod
    def lookup(cls, session, name_label):
        """Look the instance up and return it if available"""
        vm_refs = session.call_xenapi("VM.get_by_name_label", name_label)
        n = len(vm_refs)
        if n == 0:
            return None
        elif n > 1:
            raise exception.InstanceExists(name=name_label)
        else:
            return vm_refs[0]

    @classmethod
    def lookup_vm_vdis(cls, session, vm_ref):
        """Look for the VDIs that are attached to the VM"""
        # Firstly we get the VBDs, then the VDIs.
        # TODO(Armando): do we leave the read-only devices?
        vbd_refs = session.call_xenapi("VM.get_VBDs", vm_ref)
        vdi_refs = []
        if vbd_refs:
            for vbd_ref in vbd_refs:
                try:
                    vdi_ref = session.call_xenapi("VBD.get_VDI", vbd_ref)
                    # Test valid VDI
                    record = session.call_xenapi("VDI.get_record", vdi_ref)
                    LOG.debug(_('VDI %s is still available'), record['uuid'])
                except cls.XenAPI.Failure, exc:
                    LOG.exception(exc)
                else:
                    vdi_refs.append(vdi_ref)
            if len(vdi_refs) > 0:
                return vdi_refs
            else:
                return None

    @classmethod
    def preconfigure_instance(cls, session, instance, vdi_ref, network_info):
        """Makes alterations to the image before launching as part of spawn.
        """

        # As mounting the image VDI is expensive, we only want do do it once,
        # if at all, so determine whether it's required first, and then do
        # everything
        mount_required = False
        key, net, metadata = _prepare_injectables(instance, network_info)
        mount_required = key or net or metadata
        if not mount_required:
            return

        with vdi_attached_here(session, vdi_ref, read_only=False) as dev:
            _mounted_processing(dev, key, net, metadata)

    @classmethod
    def lookup_kernel_ramdisk(cls, session, vm):
        vm_rec = session.call_xenapi("VM.get_record", vm)
        if 'PV_kernel' in vm_rec and 'PV_ramdisk' in vm_rec:
            return (vm_rec['PV_kernel'], vm_rec['PV_ramdisk'])
        else:
            return (None, None)

    @classmethod
    def compile_info(cls, record):
        """Fill record with VM status information"""
        LOG.info(_("(VM_UTILS) xenserver vm state -> |%s|"),
                 record['power_state'])
        LOG.info(_("(VM_UTILS) xenapi power_state -> |%s|"),
                 XENAPI_POWER_STATE[record['power_state']])
        return {'state': XENAPI_POWER_STATE[record['power_state']],
                'max_mem': long(record['memory_static_max']) >> 10,
                'mem': long(record['memory_dynamic_max']) >> 10,
                'num_cpu': record['VCPUs_max'],
                'cpu_time': 0}

    @classmethod
    def compile_diagnostics(cls, session, record):
        """Compile VM diagnostics data"""
        try:
            host = session.get_xenapi_host()
            host_ip = session.call_xenapi("host.get_record", host)["address"]
        except (cls.XenAPI.Failure, KeyError) as e:
            return {"Unable to retrieve diagnostics": e}

        try:
            diags = {}
            xml = get_rrd(host_ip, record["uuid"])
            if xml:
                rrd = minidom.parseString(xml)
                for i, node in enumerate(rrd.firstChild.childNodes):
                    # We don't want all of the extra garbage
                    if i >= 3 and i <= 11:
                        ref = node.childNodes
                        # Name and Value
                        if len(ref) > 6:
                            diags[ref[0].firstChild.data] = \
                                ref[6].firstChild.data
            return diags
        except cls.XenAPI.Failure as e:
            return {"Unable to retrieve diagnostics": e}

    @classmethod
    def compile_metrics(cls, session, start_time, stop_time=None):
        """Compile bandwidth usage, cpu, and disk metrics for all VMs on
           this host"""
        start_time = int(start_time)
        try:
            host = session.get_xenapi_host()
            host_ip = session.call_xenapi("host.get_record", host)["address"]
        except (cls.XenAPI.Failure, KeyError) as e:
            raise exception.CouldNotFetchMetrics()

        xml = get_rrd_updates(host_ip, start_time)
        if xml:
            doc = minidom.parseString(xml)
            return parse_rrd_update(doc, start_time, stop_time)

        raise exception.CouldNotFetchMetrics()

    @classmethod
    def scan_sr(cls, session, instance=None, sr_ref=None):
        """Scans the SR specified by sr_ref"""
        if sr_ref:
            LOG.debug(_("Re-scanning SR %s"), sr_ref)
            task = session.call_xenapi('Async.SR.scan', sr_ref)
            instance_uuid = instance['uuid'] if instance else None
            session.wait_for_task(task, instance_uuid)

    @classmethod
    def scan_default_sr(cls, session):
        """Looks for the system default SR and triggers a re-scan"""
        sr_ref = cls.find_sr(session)
        session.call_xenapi('SR.scan', sr_ref)

    @classmethod
    def safe_find_sr(cls, session):
        """Same as find_sr except raises a NotFound exception if SR cannot be
        determined
        """
        sr_ref = cls.find_sr(session)
        if sr_ref is None:
            raise exception.StorageRepositoryNotFound()
        return sr_ref

    @classmethod
    def find_sr(cls, session):
        """Return the storage repository to hold VM images"""
        host = session.get_xenapi_host()

        for sr_ref, sr_rec in cls.get_all_refs_and_recs(session, 'SR'):
            if not ('i18n-key' in sr_rec['other_config'] and
                    sr_rec['other_config']['i18n-key'] == 'local-storage'):
                continue
            for pbd_ref in sr_rec['PBDs']:
                pbd_rec = cls.get_rec(session, 'PBD', pbd_ref)
                if pbd_rec and pbd_rec['host'] == host:
                    return sr_ref
        return None

    @classmethod
    def safe_find_iso_sr(cls, session):
        """Same as find_iso_sr except raises a NotFound exception if SR
        cannot be determined
        """
        sr_ref = cls.find_iso_sr(session)
        if sr_ref is None:
            raise exception.NotFound(_('Cannot find SR of content-type ISO'))
        return sr_ref

    @classmethod
    def find_iso_sr(cls, session):
        """Return the storage repository to hold ISO images"""
        host = session.get_xenapi_host()
        for sr_ref, sr_rec in cls.get_all_refs_and_recs(session, 'SR'):
            LOG.debug(_("ISO: looking at SR %(sr_rec)s") % locals())
            if not sr_rec['content_type'] == 'iso':
                LOG.debug(_("ISO: not iso content"))
                continue
            if not 'i18n-key' in sr_rec['other_config']:
                LOG.debug(_("ISO: iso content_type, no 'i18n-key' key"))
                continue
            if not sr_rec['other_config']['i18n-key'] == 'local-storage-iso':
                LOG.debug(_("ISO: iso content_type, i18n-key value not "
                            "'local-storage-iso'"))
                continue

            LOG.debug(_("ISO: SR MATCHing our criteria"))
            for pbd_ref in sr_rec['PBDs']:
                LOG.debug(_("ISO: ISO, looking to see if it is host local"))
                pbd_rec = cls.get_rec(session, 'PBD', pbd_ref)
                if not pbd_rec:
                    LOG.debug(_("ISO: PBD %(pbd_ref)s disappeared") % locals())
                    continue
                pbd_rec_host = pbd_rec['host']
                LOG.debug(_("ISO: PBD matching, want %(pbd_rec)s, " +
                            "have %(host)s") % locals())
                if pbd_rec_host == host:
                    LOG.debug(_("ISO: SR with local PBD"))
                    return sr_ref
        return None


def get_rrd(host, vm_uuid):
    """Return the VM RRD XML as a string"""
    try:
        xml = urllib.urlopen("http://%s:%s@%s/vm_rrd?uuid=%s" % (
            FLAGS.xenapi_connection_username,
            FLAGS.xenapi_connection_password,
            host,
            vm_uuid))
        return xml.read()
    except IOError:
        return None


def get_rrd_updates(host, start_time):
    """Return the RRD updates XML as a string"""
    try:
        xml = urllib.urlopen("http://%s:%s@%s/rrd_updates?start=%s" % (
            FLAGS.xenapi_connection_username,
            FLAGS.xenapi_connection_password,
            host,
            start_time))
        return xml.read()
    except IOError:
        return None


def parse_rrd_meta(doc):
    data = {}
    meta = doc.getElementsByTagName('meta')[0]
    for tag in ('start', 'end', 'step'):
        data[tag] = int(meta.getElementsByTagName(tag)[0].firstChild.data)
    legend = meta.getElementsByTagName('legend')[0]
    data['legend'] = [child.firstChild.data for child in legend.childNodes]
    return data


def parse_rrd_data(doc):
    dnode = doc.getElementsByTagName('data')[0]
    return [dict(
            time=int(child.getElementsByTagName('t')[0].firstChild.data),
            values=[Decimal(valnode.firstChild.data)
                  for valnode in child.getElementsByTagName('v')])
            for child in dnode.childNodes]


def parse_rrd_update(doc, start, until=None):
    sum_data = {}
    meta = parse_rrd_meta(doc)
    data = parse_rrd_data(doc)
    for col, collabel in enumerate(meta['legend']):
        datatype, objtype, uuid, name = collabel.split(':')
        vm_data = sum_data.get(uuid, dict())
        if name.startswith('vif'):
            vm_data[name] = integrate_series(data, col, start, until)
        else:
            vm_data[name] = average_series(data, col, start, until)
        sum_data[uuid] = vm_data
    return sum_data


def average_series(data, col, start, until=None):
    vals = [row['values'][col] for row in data
            if (not until or (row['time'] <= until)) and
                not row['values'][col].is_nan()]
    if vals:
        return (sum(vals) / len(vals)).quantize(Decimal('1.0000'))
    else:
        return Decimal('0.0000')


def integrate_series(data, col, start, until=None):
    total = Decimal('0.0000')
    prev_time = int(start)
    prev_val = None
    for row in reversed(data):
        if not until or (row['time'] <= until):
            time = row['time']
            val = row['values'][col]
            if val.is_nan():
                val = Decimal('0.0000')
            if prev_val is None:
                prev_val = val
            if prev_val >= val:
                total += ((val * (time - prev_time)) +
                          (Decimal('0.5000') * (prev_val - val) *
                          (time - prev_time)))
            else:
                total += ((prev_val * (time - prev_time)) +
                          (Decimal('0.5000') * (val - prev_val) *
                          (time - prev_time)))
            prev_time = time
            prev_val = val
    return total.quantize(Decimal('1.0000'))


#TODO(sirp): This code comes from XS5.6 pluginlib.py, we should refactor to
# use that implmenetation
def get_vhd_parent(session, vdi_rec):
    """
    Returns the VHD parent of the given VDI record, as a (ref, rec) pair.
    Returns None if we're at the root of the tree.
    """
    if 'vhd-parent' in vdi_rec['sm_config']:
        parent_uuid = vdi_rec['sm_config']['vhd-parent']
        parent_ref = session.call_xenapi("VDI.get_by_uuid", parent_uuid)
        parent_rec = session.call_xenapi("VDI.get_record", parent_ref)
        vdi_uuid = vdi_rec['uuid']
        LOG.debug(_("VHD %(vdi_uuid)s has parent %(parent_ref)s") % locals())
        return parent_ref, parent_rec
    else:
        return None


def get_vhd_parent_uuid(session, vdi_ref):
    vdi_rec = session.call_xenapi("VDI.get_record", vdi_ref)
    ret = get_vhd_parent(session, vdi_rec)
    if ret:
        parent_ref, parent_rec = ret
        return parent_rec["uuid"]
    else:
        return None


def walk_vdi_chain(session, vdi_uuid):
    """Yield vdi_recs for each element in a VDI chain"""
    # TODO(jk0): perhaps make get_vhd_parent use this
    while True:
        vdi_ref = session.call_xenapi("VDI.get_by_uuid", vdi_uuid)
        vdi_rec = session.call_xenapi("VDI.get_record", vdi_ref)
        yield vdi_rec

        parent_uuid = vdi_rec['sm_config'].get('vhd-parent')
        if parent_uuid:
            vdi_uuid = parent_uuid
        else:
            break


def _wait_for_vhd_coalesce(session, instance, sr_ref, vdi_ref,
                           original_parent_uuid):
    """ Spin until the parent VHD is coalesced into its parent VHD

    Before coalesce:
        * original_parent_vhd
            * parent_vhd
                snapshot

    Atter coalesce:
        * parent_vhd
            snapshot
    """
    max_attempts = FLAGS.xenapi_vhd_coalesce_max_attempts
    attempts = {'counter': 0}

    def _poll_vhds():
        attempts['counter'] += 1
        if attempts['counter'] > max_attempts:
            counter = attempts['counter']
            msg = (_("VHD coalesce attempts exceeded (%(counter)d >"
                    " %(max_attempts)d), giving up...") % locals())
            raise exception.Error(msg)

        VMHelper.scan_sr(session, instance, sr_ref)
        parent_uuid = get_vhd_parent_uuid(session, vdi_ref)
        if original_parent_uuid and (parent_uuid != original_parent_uuid):
            LOG.debug(_("Parent %(parent_uuid)s doesn't match original parent"
                    " %(original_parent_uuid)s, waiting for coalesce...")
                    % locals())
        else:
            # Breakout of the loop (normally) and return the parent_uuid
            raise utils.LoopingCallDone(parent_uuid)

    loop = utils.LoopingCall(_poll_vhds)
    loop.start(FLAGS.xenapi_vhd_coalesce_poll_interval, now=True)
    parent_uuid = loop.wait()
    return parent_uuid


def remap_vbd_dev(dev):
    """Return the appropriate location for a plugged-in VBD device

    Ubuntu Maverick moved xvd? -> sd?. This is considered a bug and will be
    fixed in future versions:
        https://bugs.launchpad.net/ubuntu/+source/linux/+bug/684875

    For now, we work around it by just doing a string replace.
    """
    # NOTE(sirp): This hack can go away when we pull support for Maverick
    should_remap = FLAGS.xenapi_remap_vbd_dev
    if not should_remap:
        return dev

    old_prefix = 'xvd'
    new_prefix = FLAGS.xenapi_remap_vbd_dev_prefix
    remapped_dev = dev.replace(old_prefix, new_prefix)

    return remapped_dev


def _wait_for_device(dev):
    """Wait for device node to appear"""
    for i in xrange(0, FLAGS.block_device_creation_timeout):
        dev_path = utils.make_dev_path(dev)
        if os.path.exists(dev_path):
            return
        time.sleep(1)

    raise volume_utils.StorageError(
        _('Timeout waiting for device %s to be created') % dev)


@contextlib.contextmanager
def vdi_attached_here(session, vdi_ref, read_only=False):
    this_vm_ref = get_this_vm_ref(session)
    vbd_rec = {}
    vbd_rec['VM'] = this_vm_ref
    vbd_rec['VDI'] = vdi_ref
    vbd_rec['userdevice'] = 'autodetect'
    vbd_rec['bootable'] = False
    vbd_rec['mode'] = read_only and 'RO' or 'RW'
    vbd_rec['type'] = 'disk'
    vbd_rec['unpluggable'] = True
    vbd_rec['empty'] = False
    vbd_rec['other_config'] = {}
    vbd_rec['qos_algorithm_type'] = ''
    vbd_rec['qos_algorithm_params'] = {}
    vbd_rec['qos_supported_algorithms'] = []
    LOG.debug(_('Creating VBD for VDI %s ... '), vdi_ref)
    vbd_ref = session.call_xenapi("VBD.create", vbd_rec)
    LOG.debug(_('Creating VBD for VDI %s done.'), vdi_ref)
    try:
        LOG.debug(_('Plugging VBD %s ... '), vbd_ref)
        session.call_xenapi("VBD.plug", vbd_ref)
        try:
            LOG.debug(_('Plugging VBD %s done.'), vbd_ref)
            orig_dev = session.call_xenapi("VBD.get_device", vbd_ref)
            LOG.debug(_('VBD %(vbd_ref)s plugged as %(orig_dev)s') % locals())
            dev = remap_vbd_dev(orig_dev)
            if dev != orig_dev:
                LOG.debug(_('VBD %(vbd_ref)s plugged into wrong dev, '
                            'remapping to %(dev)s') % locals())
            if dev != 'autodetect':
                # NOTE(johannes): Unit tests will end up with a device called
                # 'autodetect' which obviously won't exist. It's not ideal,
                # but the alternatives were much messier
                _wait_for_device(dev)
            yield dev
        finally:
            LOG.debug(_('Destroying VBD for VDI %s ... '), vdi_ref)
            vbd_unplug_with_retry(session, vbd_ref)
    finally:
        ignore_failure(session.call_xenapi, "VBD.destroy", vbd_ref)
        LOG.debug(_('Destroying VBD for VDI %s done.'), vdi_ref)


def vbd_unplug_with_retry(session, vbd_ref):
    """Call VBD.unplug on the given VBD, with a retry if we get
    DEVICE_DETACH_REJECTED.  For reasons which I don't understand, we're
    seeing the device still in use, even when all processes using the device
    should be dead."""
    # FIXME(sirp): We can use LoopingCall here w/o blocking sleep()
    while True:
        try:
            session.call_xenapi("VBD.unplug", vbd_ref)
            LOG.debug(_('VBD.unplug successful first time.'))
            return
        except VMHelper.XenAPI.Failure, e:
            if (len(e.details) > 0 and
                e.details[0] == 'DEVICE_DETACH_REJECTED'):
                LOG.debug(_('VBD.unplug rejected: retrying...'))
                time.sleep(1)
                LOG.debug(_('Not sleeping anymore!'))
            elif (len(e.details) > 0 and
                  e.details[0] == 'DEVICE_ALREADY_DETACHED'):
                LOG.debug(_('VBD.unplug successful eventually.'))
                return
            else:
                LOG.error(_('Ignoring XenAPI.Failure in VBD.unplug: %s'),
                              e)
                return


def ignore_failure(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except VMHelper.XenAPI.Failure, e:
        LOG.error(_('Ignoring XenAPI.Failure %s'), e)
        return None


def get_this_vm_uuid():
    with file('/sys/hypervisor/uuid') as f:
        return f.readline().strip()


def get_this_vm_ref(session):
    return session.call_xenapi("VM.get_by_uuid", get_this_vm_uuid())


def _is_vdi_pv(dev):
    LOG.debug(_("Running pygrub against %s"), dev)
    dev_path = utils.make_dev_path(dev)
    output = os.popen('pygrub -qn %s' % dev_path)
    for line in output.readlines():
        #try to find kernel string
        m = re.search('(?<=kernel:)/.*(?:>)', line)
        if m and m.group(0).find('xen') != -1:
            LOG.debug(_("Found Xen kernel %s") % m.group(0))
            return True
    LOG.debug(_("No Xen kernel found.  Booting HVM."))
    return False


def _get_partitions(dev):
    """Return partition information (num, size, type) for a device."""
    dev_path = utils.make_dev_path(dev)
    out, err = utils.execute('parted', '--script', '--machine',
                             dev_path, 'unit s', 'print',
                             run_as_root=True)
    lines = [line for line in out.split('\n') if line]
    partitions = []

    LOG.debug(_("Partitions:"))
    for line in lines[2:]:
        num, start, end, size, ptype = line.split(':')[:5]
        start = int(start.rstrip('s'))
        end = int(end.rstrip('s'))
        size = int(size.rstrip('s'))
        LOG.debug(_("  %(num)s: %(ptype)s %(size)d sectors") % locals())
        partitions.append((num, start, size, ptype))

    return partitions


def _stream_disk(dev, image_type, virtual_size, image_file):
    offset = 0
    if image_type == ImageType.DISK:
        offset = MBR_SIZE_BYTES
        _write_partition(virtual_size, dev)

    dev_path = utils.make_dev_path(dev)
    utils.execute('chown', os.getuid(), dev_path, run_as_root=True)

    with open(dev_path, 'wb') as f:
        f.seek(offset)
        for chunk in image_file:
            f.write(chunk)


def _write_partition(virtual_size, dev):
    dev_path = utils.make_dev_path(dev)
    primary_first = MBR_SIZE_SECTORS
    primary_last = MBR_SIZE_SECTORS + (virtual_size / SECTOR_SIZE) - 1

    LOG.debug(_('Writing partition table %(primary_first)d %(primary_last)d'
            ' to %(dev_path)s...') % locals())

    def execute(*cmd, **kwargs):
        return utils.execute(*cmd, **kwargs)

    execute('parted', '--script', dev_path, 'mklabel', 'msdos',
            run_as_root=True)
    execute('parted', '--script', dev_path, 'mkpart', 'primary',
            '%ds' % primary_first,
            '%ds' % primary_last,
            run_as_root=True)

    LOG.debug(_('Writing partition table %s done.'), dev_path)


def _resize_part_and_fs(dev, start, old_sectors, new_sectors):
    """Resize partition and fileystem.

    This assumes we are dealing with a single primary partition and using
    ext3 or ext4.
    """
    size = new_sectors - start
    end = new_sectors - 1

    dev_path = utils.make_dev_path(dev)
    partition_path = utils.make_dev_path(dev, partition=1)

    # Remove ext3 journal (making it ext2)
    utils.execute('tune2fs', '-O ^has_journal', partition_path,
                  run_as_root=True)

    # fsck the disk
    # NOTE(sirp): using -p here to automatically repair filesystem, is
    # this okay?
    utils.execute('e2fsck', '-f', '-p', partition_path, run_as_root=True)

    if new_sectors < old_sectors:
        # Resizing down, resize filesystem before partition resize
        utils.execute('resize2fs', partition_path, '%ds' % size,
                      run_as_root=True)

    utils.execute('parted', '--script', dev_path, 'rm', '1',
                  run_as_root=True)
    utils.execute('parted', '--script', dev_path, 'mkpart',
                  'primary',
                  '%ds' % start,
                  '%ds' % end,
                  run_as_root=True)

    if new_sectors > old_sectors:
        # Resizing up, resize filesystem after partition resize
        utils.execute('resize2fs', partition_path, run_as_root=True)

    # Add back journal
    utils.execute('tune2fs', '-j', partition_path, run_as_root=True)


def _copy_partition(session, src_ref, dst_ref, partition, virtual_size):
    # Part of disk taken up by MBR
    virtual_size -= MBR_SIZE_BYTES

    with vdi_attached_here(session, src_ref, read_only=True) as src:
        src_path = utils.make_dev_path(src, partition=partition)

        with vdi_attached_here(session, dst_ref, read_only=False) as dst:
            dst_path = utils.make_dev_path(dst, partition=partition)

            _write_partition(virtual_size, dst)

            num_blocks = virtual_size / SECTOR_SIZE
            utils.execute('dd',
                          'if=%s' % src_path,
                          'of=%s' % dst_path,
                          'count=%d' % num_blocks,
                          run_as_root=True)


def _mount_filesystem(dev_path, dir):
    """mounts the device specified by dev_path in dir"""
    try:
        out, err = utils.execute('mount',
                                 '-t', 'ext2,ext3,ext4,reiserfs',
                                 dev_path, dir, run_as_root=True)
    except exception.ProcessExecutionError as e:
        err = str(e)
    return err


def _find_guest_agent(base_dir, agent_rel_path):
    """
    tries to locate a guest agent at the path
    specificed by agent_rel_path
    """
    agent_path = os.path.join(base_dir, agent_rel_path)
    if os.path.isfile(agent_path):
        # The presence of the guest agent
        # file indicates that this instance can
        # reconfigure the network from xenstore data,
        # so manipulation of files in /etc is not
        # required
        LOG.info(_('XenServer tools installed in this '
                'image are capable of network injection.  '
                'Networking files will not be'
                'manipulated'))
        return True
    xe_daemon_filename = os.path.join(base_dir,
        'usr', 'sbin', 'xe-daemon')
    if os.path.isfile(xe_daemon_filename):
        LOG.info(_('XenServer tools are present '
                'in this image but are not capable '
                'of network injection'))
    else:
        LOG.info(_('XenServer tools are not '
                'installed in this image'))
    return False


def _mounted_processing(device, key, net, metadata):
    """Callback which runs with the image VDI attached"""
    # NB: Partition 1 hardcoded
    dev_path = utils.make_dev_path(device, partition=1)
    tmpdir = tempfile.mkdtemp()
    try:
        # Mount only Linux filesystems, to avoid disturbing NTFS images
        err = _mount_filesystem(dev_path, tmpdir)
        if not err:
            try:
                # This try block ensures that the umount occurs
                if not _find_guest_agent(tmpdir, FLAGS.xenapi_agent_path):
                    LOG.info(_('Manipulating interface files '
                            'directly'))
                    disk.inject_data_into_fs(tmpdir, key, net, metadata,
                        utils.execute)
            finally:
                utils.execute('umount', dev_path, run_as_root=True)
        else:
            LOG.info(_('Failed to mount filesystem (expected for '
                'non-linux instances): %s') % err)
    finally:
        # remove temporary directory
        os.rmdir(tmpdir)


def _prepare_injectables(inst, networks_info):
    """
    prepares the ssh key and the network configuration file to be
    injected into the disk image
    """
    #do the import here - Cheetah.Template will be loaded
    #only if injection is performed
    from Cheetah import Template as t
    template = t.Template
    template_data = open(FLAGS.injected_network_template).read()

    metadata = inst['metadata']
    key = str(inst['key_data'])
    net = None
    if networks_info:
        ifc_num = -1
        interfaces_info = []
        have_injected_networks = False
        for (network_ref, info) in networks_info:
            ifc_num += 1
            if not network_ref['injected']:
                continue

            have_injected_networks = True
            ip_v4 = ip_v6 = None
            if 'ips' in info and len(info['ips']) > 0:
                ip_v4 = info['ips'][0]
            if 'ip6s' in info and len(info['ip6s']) > 0:
                ip_v6 = info['ip6s'][0]
            if len(info['dns']) > 0:
                dns = info['dns'][0]
            else:
                dns = ''
            interface_info = {'name': 'eth%d' % ifc_num,
                              'address': ip_v4 and ip_v4['ip'] or '',
                              'netmask': ip_v4 and ip_v4['netmask'] or '',
                              'gateway': info['gateway'],
                              'broadcast': info['broadcast'],
                              'dns': dns,
                              'address_v6': ip_v6 and ip_v6['ip'] or '',
                              'netmask_v6': ip_v6 and ip_v6['netmask'] or '',
                              'gateway_v6': ip_v6 and info['gateway6'] or '',
                              'use_ipv6': FLAGS.use_ipv6}
            interfaces_info.append(interface_info)

        if have_injected_networks:
            net = str(template(template_data,
                                searchList=[{'interfaces': interfaces_info,
                                            'use_ipv6': FLAGS.use_ipv6}]))
    return key, net, metadata
