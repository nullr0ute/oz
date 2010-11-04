# Copyright (C) 2010  Chris Lalancette <clalance@redhat.com>

# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 2.1 of the License, or (at your option) any later version.

# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public
# License along with this library; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301  USA

import uuid
import libvirt
import os
import subprocess
import shutil
import time
import xml.dom.minidom
import pycurl
import sys
import urllib
import re
import stat
import urlparse
import httplib
import ozutil
import libxml2
import logging
import random
import guestfs
import socket
import select

class ProcessError(Exception):
    """This exception is raised when a process run by
    Guest.subprocess_check_output returns a non-zero exit status.  The exit
    status will be stored in the returncode attribute
    """
    def __init__(self, returncode, cmd, output=None):
        self.returncode = returncode
        self.cmd = cmd
        self.output = output
    def __str__(self):
        return "'%s' failed(%d): %s" % (self.cmd, self.returncode, self.output)

# NOTE: python 2.7 already defines subprocess.capture_output, but I can't
# depend on that yet.  So write my own
def subprocess_check_output(*popenargs, **kwargs):
    if 'stdout' in kwargs:
        raise ValueError('stdout argument not allowed, it will be overridden.')

    ozutil.executable_exists(popenargs[0][0])

    process = subprocess.Popen(stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, *popenargs, **kwargs)
    output, unused_err = process.communicate()
    retcode = process.poll()
    if retcode:
        cmd = ' '.join(*popenargs)
        raise ProcessError(retcode, cmd, output=output)
    return output

class NullHandler(logging.Handler):
    def emit(self, record):
        pass

class Guest(object):
    def __init__(self, distro, update, arch, nicmodel, clockoffset, mousetype,
                 diskbus):
        if arch != "i386" and arch != "x86_64":
            raise Exception, "Unsupported guest arch " + arch
        self.log = logging.getLogger('%s.%s' % (__name__, self.__class__.__name__))
        self.uuid = uuid.uuid4()
        mac = [0x52, 0x54, 0x00, random.randint(0x00, 0xff),
               random.randint(0x00, 0xff), random.randint(0x00, 0xff)]
        self.macaddr = ':'.join(map(lambda x:"%02x" % x, mac))
        self.distro = distro
        self.update = update
        self.arch = arch
        self.name = self.distro + self.update + self.arch
        self.diskimage = "/var/lib/libvirt/images/" + self.name + ".dsk"
        self.cdl_tmp = "/var/lib/oz/cdltmp/" + self.name
        self.listen_port = random.randrange(1024, 65535)
        self.libvirt_conn = libvirt.open("qemu:///system")

        # we have to make sure that the private libvirt bridge is available
        self.host_bridge_ip = None
        for netname in self.libvirt_conn.listNetworks():
            network = self.libvirt_conn.networkLookupByName(netname)
            if network.bridgeName() == 'virbr0':
                xml = network.XMLDesc(0)
                doc = libxml2.parseMemory(xml, len(xml))
                ip = doc.xpathEval('/network/ip')
                if len(ip) != 1:
                    raise Exception, "Failed to find host IP address for virbr0"
                self.host_bridge_ip = ip[0].prop('address')
                break
        if self.host_bridge_ip is None:
            raise Exception, "Default libvirt network (virbr0) does not exist, install cannot continue"

        self.nicmodel = nicmodel
        if self.nicmodel is None:
            self.nicmodel = "rtl8139"
        self.clockoffset = clockoffset
        if self.clockoffset is None:
            self.clockoffset = "utc"
        self.mousetype = mousetype
        if self.mousetype is None:
            self.mousetype = "ps2"
        if diskbus is None or diskbus == "ide":
            self.disk_bus = "ide"
            self.disk_dev = "hda"
        elif diskbus == "virtio":
            self.disk_bus = "virtio"
            self.disk_dev = "vda"
        else:
            raise Exception, "Unknown diskbus type " + diskbus

        self.log.debug("Name: %s, UUID: %s, MAC: %s, distro: %s" % (self.name, self.uuid, self.macaddr, self.distro))
        self.log.debug("update: %s, arch: %s, diskimage: %s" % (self.update, self.arch, self.diskimage))
        self.log.debug("host IP: %s, nicmodel: %s, clockoffset: %s" % (self.host_bridge_ip, self.nicmodel, self.clockoffset))
        self.log.debug("mousetype: %s, disk_bus: %s, disk_dev: %s" % (self.mousetype, self.disk_bus, self.disk_dev))
        self.log.debug("cdltmp: %s, listen_port: %d" % (self.cdl_tmp, self.listen_port))

    def cleanup_old_guest(self):
        def handler(ctxt, err):
            pass
        libvirt.registerErrorHandler(handler, 'context')
        self.log.info("Cleaning up old guest named %s" % (self.name))
        try:
            dom = self.libvirt_conn.lookupByName(self.name)
            try:
                dom.destroy()
            except:
                pass
            dom.undefine()
        except:
            pass
        libvirt.registerErrorHandler(None, None)

        # FIXME: do we really want to remove this here?
        if os.access(self.diskimage, os.F_OK):
            os.unlink(self.diskimage)

    def targetDev(self, doc, devicetype, path, bus):
        installNode = doc.createElement("disk")
        installNode.setAttribute("type", "file")
        installNode.setAttribute("device", devicetype)
        sourceInstallNode = doc.createElement("source")
        sourceInstallNode.setAttribute("file", path)
        installNode.appendChild(sourceInstallNode)
        targetInstallNode = doc.createElement("target")
        targetInstallNode.setAttribute("dev", bus)
        installNode.appendChild(targetInstallNode)
        return installNode

    def generate_define_xml(self, bootdev):
        self.log.info("Generate/define XML for guest %s with bootdev %s" % (self.name, bootdev))

        # create top-level domain element
        doc = xml.dom.minidom.Document()
        domain = doc.createElement("domain")
        domain.setAttribute("type", "kvm")
        doc.appendChild(domain)

        # create name element
        nameNode = doc.createElement("name")
        nameNode.appendChild(doc.createTextNode(self.name))
        domain.appendChild(nameNode)

        # create memory nodes
        memoryNode = doc.createElement("memory")
        currentMemoryNode = doc.createElement("currentMemory")
        memoryNode.appendChild(doc.createTextNode(str(1024 * 1024)))
        currentMemoryNode.appendChild(doc.createTextNode(str(1024 * 1024)))
        domain.appendChild(memoryNode)
        domain.appendChild(currentMemoryNode)

        # create uuid
        uuidNode = doc.createElement("uuid")
        uuidNode.appendChild(doc.createTextNode(str(self.uuid)))
        domain.appendChild(uuidNode)

        # clock offset
        offsetNode = doc.createElement("clock")
        offsetNode.setAttribute("offset", self.clockoffset)
        domain.appendChild(offsetNode)

        # create vcpu
        vcpusNode = doc.createElement("vcpu")
        vcpusNode.appendChild(doc.createTextNode(str(1)))
        domain.appendChild(vcpusNode)

        # create features
        featuresNode = doc.createElement("features")
        acpiNode = doc.createElement("acpi")
        apicNode = doc.createElement("apic")
        paeNode = doc.createElement("pae")
        featuresNode.appendChild(acpiNode)
        featuresNode.appendChild(apicNode)
        featuresNode.appendChild(paeNode)
        domain.appendChild(featuresNode)

        # create os
        osNode = doc.createElement("os")
        typeNode = doc.createElement("type")
        typeNode.appendChild(doc.createTextNode("hvm"))
        osNode.appendChild(typeNode)
        bootNode = doc.createElement("boot")
        bootNode.setAttribute("dev", bootdev)
        osNode.appendChild(bootNode)
        domain.appendChild(osNode)

        # create poweroff, reboot, crash nodes
        poweroffNode = doc.createElement("on_poweroff")
        rebootNode = doc.createElement("on_reboot")
        crashNode = doc.createElement("on_crash")
        poweroffNode.appendChild(doc.createTextNode("destroy"))
        rebootNode.appendChild(doc.createTextNode("destroy"))
        crashNode.appendChild(doc.createTextNode("destroy"))
        domain.appendChild(poweroffNode)
        domain.appendChild(rebootNode)
        domain.appendChild(crashNode)

        # create devices section
        devicesNode = doc.createElement("devices")
        # console
        consoleNode = doc.createElement("console")
        consoleNode.setAttribute("device", "pty")
        devicesNode.appendChild(consoleNode)
        # graphics
        graphicsNode = doc.createElement("graphics")
        graphicsNode.setAttribute("type", "vnc")
        graphicsNode.setAttribute("port", "-1")
        devicesNode.appendChild(graphicsNode)
        # network
        interfaceNode = doc.createElement("interface")
        interfaceNode.setAttribute("type", "bridge")
        sourceNode = doc.createElement("source")
        sourceNode.setAttribute("bridge", "virbr0")
        interfaceNode.appendChild(sourceNode)
        macNode = doc.createElement("mac")
        macNode.setAttribute("address", self.macaddr)
        interfaceNode.appendChild(macNode)
        modelNode = doc.createElement("model")
        modelNode.setAttribute("type", self.nicmodel)
        interfaceNode.appendChild(modelNode)
        devicesNode.appendChild(interfaceNode)
        # input
        inputNode = doc.createElement("input")
        if self.mousetype == "ps2":
            inputNode.setAttribute("type", "mouse")
            inputNode.setAttribute("bus", "ps2")
        elif self.mousetype == "usb":
            inputNode.setAttribute("type", "tablet")
            inputNode.setAttribute("bus", "usb")
        devicesNode.appendChild(inputNode)
        # console
        consoleNode = doc.createElement("console")
        consoleNode.setAttribute("type", "pty")
        targetConsoleNode = doc.createElement("target")
        targetConsoleNode.setAttribute("port", "0")
        consoleNode.appendChild(targetConsoleNode)
        devicesNode.appendChild(consoleNode)
        # boot disk
        diskNode = doc.createElement("disk")
        diskNode.setAttribute("type", "file")
        diskNode.setAttribute("device", "disk")
        targetNode = doc.createElement("target")
        targetNode.setAttribute("dev", self.disk_dev)
        targetNode.setAttribute("bus", self.disk_bus)
        diskNode.appendChild(targetNode)
        sourceDiskNode = doc.createElement("source")
        sourceDiskNode.setAttribute("file", self.diskimage)
        diskNode.appendChild(sourceDiskNode)
        devicesNode.appendChild(diskNode)
        # install disk (cdrom or floppy)
        if hasattr(self, "output_iso"):
            devicesNode.appendChild(self.targetDev(doc, "cdrom", self.output_iso, "hdc"))
        if hasattr(self, "output_floppy"):
            devicesNode.appendChild(self.targetDev(doc, "floppy", self.output_floppy, "fda"))
        domain.appendChild(devicesNode)

        self.log.debug("Generated XML:\n%s" % (doc.toxml()))

        self.libvirt_dom = self.libvirt_conn.defineXML(doc.toxml())

    def generate_blank_diskimage(self, size=10):
        self.log.info("Generating %dGB blank diskimage for %s" % (size, self.name))
        f = open(self.diskimage, "w")
        # 10 GB disk image by default
        f.truncate(size * 1024 * 1024 * 1024)
        f.close()

    def generate_diskimage(self, size=10):
        self.log.info("Generating %dGB diskimage with fake partition for %s" % (size, self.name))
        # FIXME: I think that this partition table will only work with the 10GB
        # image.  We'll need to do something more sophisticated when we handle
        # variable sized disks
        f = open(self.diskimage, "w")
        f.seek(0x1bf)
        f.write("\x01\x01\x00\x82\xfe\x3f\x7c\x3f\x00\x00\x00\xfe\xa3\x1e")
        f.seek(0x1fe)
        f.write("\x55\xaa")
        f.seek(size * 1024 * 1024 * 1024)
        f.write("\x00")
        f.close()

    def wait_for_install_finish(self, count):
        lastlen = 0
        origcount = count
        while count > 0:
            try:
                if count % 10 == 0:
                    self.log.info("Waiting for %s to finish installing, %d/%d" % (self.name, count, origcount))
                info = self.libvirt_dom.info()
                if info[0] != libvirt.VIR_DOMAIN_RUNNING and info[0] != libvirt.VIR_DOMAIN_BLOCKED:
                    break
                count -= 1
            except:
                pass
            time.sleep(1)

        if count == 0:
            # if we timed out, then let's make sure to take a screenshot.
            # FIXME: where should we put this screenshot?
            screenshot = self.name + "-" + str(time.time()) + ".png"
            self.capture_screenshot(self.libvirt_dom.XMLDesc(0), screenshot)
            raise Exception, "Timed out waiting for install to finish"

    def get_original_media(self, url, output):
        original_available = False

        # note that all redirects should already have been resolved by
        # this point; this is merely to check that the media that we are
        # trying to fetch actually exists
        conn = urllib.urlopen(url)
        if conn.getcode() != 200:
            raise Exception, "Could not access install url: " + conn.getcode()

        if os.access(output, os.F_OK):
            try:
                for header in conn.headers.headers:
                    if re.match("Content-Length:", header):
                        if int(header.split()[1]) == os.stat(output)[stat.ST_SIZE]:
                            original_available = True
                        break
            except:
                # if any of the above failed, then the worst case is that we
                # re-download something we didn't need to.  So just go on
                pass

        conn.close()

        if original_available:
            self.log.info("Original install media available, using cached version")
        else:
            self.log.info("Fetching the original install media from %s" % (url))
            def progress(down_total, down_current, up_total, up_current):
                self.log.info("%dkB of %dkB" % (down_current/1024, down_total/1024))

            if not os.access(os.path.dirname(output), os.F_OK):
                os.makedirs(os.path.dirname(output))
            self.outf = open(output, "w")
            def data(buf):
                self.outf.write(buf)

            c = pycurl.Curl()
            c.setopt(c.URL, url)
            c.setopt(c.CONNECTTIMEOUT, 5)
            c.setopt(c.WRITEFUNCTION, data)
            c.setopt(c.NOPROGRESS, 0)
            c.setopt(c.PROGRESSFUNCTION, progress)
            c.perform()
            c.close()
            self.outf.close()

            if os.stat(output)[stat.ST_SIZE] == 0:
                # if we see a zero-sized media after the download, we know
                # something went wrong
                raise Exception, "Media of 0 size downloaded"

    def capture_screenshot(self, xml, filename):
        doc = libxml2.parseMemory(xml, len(xml))
        graphics = doc.xpathEval('/domain/devices/graphics')
        if len(graphics) != 1:
            self.log.error("Could not find the VNC port")
            return

        if graphics[0].prop('type') != 'vnc':
            self.log.error("Graphics type is not VNC, not taking screenshot")
            return

        port = graphics[0].prop('port')

        if port is None:
            self.log.error("Port is not specified, not taking screenshot")
            return

        vnc = "localhost:%s" % (int(port) - 5900)

        # we don't use subprocess_check_output here because if this fails,
        # we don't want to raise an exception, just print an error
        ret = subprocess.call(['gvnccapture', vnc, filename], stdout=open('/dev/null', 'w'), stderr=subprocess.STDOUT)
        if ret != 0:
            self.log.error("Failed to take screenshot")

    def guestfs_handle_setup(self, disk):
        for domid in self.libvirt_conn.listDomainsID():
            self.log.debug("DomID: %d" % (domid))
            dom = self.libvirt_conn.lookupByID(domid)
            xml = dom.XMLDesc(0)
            doc = libxml2.parseMemory(xml, len(xml))
            disks = doc.xpathEval('/domain/devices/disk')
            if len(disks) < 1:
                # odd, a domain without a disk, but don't worry about it
                continue
            for guestdisk in disks:
                for source in guestdisk.xpathEval("source"):
                    filename = str(source.prop('file'))
                    if filename == disk:
                        raise Exception, "Cannot setup CDL generation on a running disk"

        self.log.info("Setting up guestfs handle for %s" % (self.name))
        self.g = guestfs.GuestFS()

        self.log.debug("Adding disk image %s" % (disk))
        self.g.add_drive(disk)

        self.log.debug("Launching guestfs")
        self.g.launch()

        self.log.debug("Inspecting guest OS")
        os = self.g.inspect_os()

        self.log.debug("Getting mountpoints")
        mountpoints = self.g.inspect_get_mountpoints(os[0])

        self.log.debug("Mounting /")
        for point in mountpoints:
            if point[0] == '/':
                self.g.mount(point[1], '/')
                break

        self.log.debug("Mount other filesystems")
        for point in mountpoints:
            if point[0] != '/':
                self.g.mount(point[1], point[0])

    def guestfs_handle_cleanup(self):
        self.log.info("Cleaning up guestfs handle for %s" % (self.name))
        self.log.debug("Syncing")
        self.g.sync()

        self.log.debug("Unmounting all")
        self.g.umount_all()

        self.log.debug("Killing guestfs subprocess")
        self.g.kill_subprocess()

    def wait_for_guest_boot(self):
        self.log.info("Listening on %d for %s to boot" % (self.listen_port, self.name))

        listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen.bind((self.host_bridge_ip, self.listen_port))
        listen.listen(1)
        # FIXME: we should make this iptables rule only open the port on the
        # virbr0 interface
        # FIXME: we should use the subprocess_check_output wrapper
        subprocess.call(["iptables", "-I", "INPUT", "1", "-p", "tcp", "-m",
                         "tcp", "--dport", str(self.listen_port), "-j",
                         "ACCEPT"])

        try:
            rlist, wlist, xlist = select.select([listen], [], [], 300)
        finally:
            subprocess.call(["iptables", "-D", "INPUT", "1"])
        if len(rlist) == 0:
            raise Exception, "Timed out waiting for domain to boot"
        new_sock, addr = listen.accept()
        new_sock.close()
        listen.close()

        self.log.debug("IP address of guest is %s" % (addr[0]))

        return addr[0]

    def output_cdl_xml(self, lines):
        doc = xml.dom.minidom.Document()
        cdl = doc.createElement("cdl")
        doc.appendChild(cdl)

        packagesNode = doc.createElement("packages")
        cdl.appendChild(packagesNode)

        for line in lines:
            if line == "":
                continue
            packageNode = doc.createElement("package")
            packageNode.setAttribute("name", line)
            packagesNode.appendChild(packageNode)

        return doc.toxml()

class CDGuest(Guest):
    def __init__(self, distro, update, arch, nicmodel, clockoffset, mousetype,
                 diskbus):
        Guest.__init__(self, distro, update, arch, nicmodel, clockoffset, mousetype, diskbus)
        self.orig_iso = "/var/lib/oz/isos/" + self.name + ".iso"
        self.output_iso = "/var/lib/libvirt/images/" + self.name + "-oz.iso"
        self.iso_contents = "/var/lib/oz/isocontent/" + self.name

    def get_original_iso(self, isourl):
        return self.get_original_media(isourl, self.orig_iso)

    def copy_iso(self):
        self.log.info("Copying ISO contents for modification")
        isomount = "/var/lib/oz/mnt/" + self.name
        if os.access(isomount, os.F_OK):
            os.rmdir(isomount)
        os.makedirs(isomount)

        if os.access(self.iso_contents, os.F_OK):
            shutil.rmtree(self.iso_contents)

        # mount and copy the ISO
        subprocess_check_output(["fuseiso", self.orig_iso, isomount])

        try:
            shutil.copytree(isomount, self.iso_contents, symlinks=True)
        finally:
            # if fusermount fails, there is not much we can do.  Print an
            # error, but go on anyway
            if subprocess.call(["fusermount", "-u", isomount]) != 0:
                self.log.error("Failed to unmount ISO; continuing anyway")
            os.rmdir(isomount)

    def install(self):
        self.log.info("Running install for %s" % (self.name))
        self.generate_define_xml("cdrom")
        self.libvirt_dom.create()

        self.wait_for_install_finish(1200)

        self.generate_define_xml("hd")

    def cleanup_iso(self):
        self.log.info("Cleaning up old ISO data")
        shutil.rmtree(self.iso_contents)

    def cleanup_install(self):
        self.log.info("Cleaning up modified ISO")
        os.unlink(self.output_iso)

class FDGuest(Guest):
    def __init__(self, distro, update, arch, nicmodel, clockoffset, mousetype,
                 diskbus):
        Guest.__init__(self, distro, update, arch, nicmodel, clockoffset, mousetype, diskbus)
        self.orig_floppy = "/var/lib/oz/floppies/" + self.name + ".img"
        self.output_floppy = "/var/lib/libvirt/images/" + self.name + "-oz.img"
        self.floppy_contents = "/var/lib/oz/floppycontent/" + self.name

    def get_original_floppy(self, floppyurl):
        return self.get_original_media(floppyurl, self.orig_floppy)

    def copy_floppy(self):
        self.log.info("Copying floppy contents for modification")
        shutil.copyfile(self.orig_floppy, self.output_floppy)

    def install(self):
        self.log.info("Running install for %s" % (self.name))
        self.generate_define_xml("fd")
        self.libvirt_dom.create()

        self.wait_for_install_finish(1200)

        self.generate_define_xml("hd")

    def cleanup_floppy(self):
        self.log.info("Cleaning up floppy data")
        shutil.rmtree(self.floppy_contents)

    def cleanup_install(self):
        self.log.info("Cleaning up modified floppy")
        os.unlink(self.output_floppy)