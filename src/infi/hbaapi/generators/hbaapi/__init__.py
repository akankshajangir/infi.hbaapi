__import__("pkg_resources").declare_namespace(__name__)

import logging
from infi.os_info import get_platform_string
from contextlib import contextmanager
from munch import Munch
import ctypes
from .. import Generator
from ... import Port, PortStatistics, FC_PORT_STATISTICS
from infi.dtypes.wwn import WWN
from . import c_api, headers
import binascii
import sys

log = logging.getLogger(__name__)

WELL_KNOWN_FC_ADDRESSES = [
    0xFFFFF5, # Multicast server
    0xFFFFF6, # Clock Sync server
    0xFFFFF7, # KDC (key distribution)
    0xFFFFF8, # Alias server (for multicast, or hunt groups)
    0xFFFFF9, # QoS information
    0xFFFFFA, # Management server
    0xFFFFFB, # Time server
    0xFFFFFC, # Directory server
    0xFFFFFD, # Fabric Controller
    0xFFFFFE, # Fabric Login server
]

PY3 = sys.version_info[0] == 3


class HbaApi(Generator):
    def __init__(self):
        Generator.__init__(self)

    def _iter_adapters(self):
        for adapter_index in range (0, c_api.HBA_GetNumberOfAdapters()):
            adapter_name = ctypes.create_string_buffer(headers.MAX_ADAPTERNAME_LENGTH)
            c_api.HBA_GetAdapterName(adapter_index, adapter_name)
            log.debug("yield adapter index {} name {!r}".format(adapter_index, adapter_name.value))
            yield adapter_name

    def _get_adapter_attributes(self, adapter_handle):
        buff = ctypes.c_buffer(headers.HBA_AdapterAttributes.min_max_sizeof().max) #pylint: disable-msg=W0622,E1101
        c_api.HBA_GetAdapterAttributes(adapter_handle, buff)
        adapter_attributes = headers.HBA_AdapterAttributes.create_from_string(buff.raw) #pylint: disable-msg=E1101
        return adapter_attributes

    def _get_port_attributes(self, adapter_handle, port_index):
        buff = ctypes.c_buffer(headers.HBA_PortAttributes.min_max_sizeof().max) #pylint: disable-msg=W0622,E1101
        c_api.HBA_GetAdapterPortAttributes(adapter_handle, port_index, buff)
        port_attributes = headers.HBA_PortAttributes.create_from_string(buff.raw) #pylint: disable-msg=E1101
        return port_attributes

    def _get_remote_port_attributes(self, adapter_handle, port_index, remote_port_index):
        buff = ctypes.c_buffer(headers.HBA_PortAttributes.min_max_sizeof().max) #pylint: disable-msg=W0622,E1101
        c_api.HBA_GetDiscoveredPortAttributes(adapter_handle, port_index, remote_port_index, buff)
        remote_port_attributes = headers.HBA_PortAttributes.create_from_string(buff.raw) #pylint: disable-msg=E1101
        return remote_port_attributes

    def _get_remote_ports(self, adapter_handle, port_index, number_of_remote_ports):
        remote_ports = []
        for remote_port_index in range(0, number_of_remote_ports):
            remote_port_attributes = self._get_remote_port_attributes(adapter_handle, port_index, remote_port_index)
            remote_port = get_port_object(None, remote_port_attributes)
            log.debug("Found remote port {!r}".format(remote_port.port_wwn))
            if remote_port.port_fcid in WELL_KNOWN_FC_ADDRESSES:
                msg = "port {!r} is a well-known FC address with fcid {!r}"
                log.debug(msg.format(remote_port.port_wwn, remote_port.port_fcid))
            elif remote_port.port_state == 'offline':
                msg = "remote port {!r} is offline"
                log.debug(msg.format(remote_port.port_wwn))
            else:
                remote_ports.append(remote_port)
        return remote_ports

    def _populate_local_port_hct(self, port):
        from re import compile
        pattern = compile(br"(?P<host>\d+)$")
        if port.os_device_name is None:
            # in case we didn't get the os_device_name
            port.hct = (-1, -1, -1)
            return
        match = pattern.search(port.os_device_name.strip(b':'))
        if match is None:
            # no match
            port.hct = (-1, -1, -1)
            return
        result = match.groupdict()
        port.hct = (int(result['host']), -1, -1)

    def _get_local_port(self, adapter_handle, adapter_attributes, port_index):
        port_attributes = self._get_port_attributes(adapter_handle, port_index)
        port = get_port_object(adapter_attributes, port_attributes)
        log.debug("checking local fc port {!r}".format(port.port_wwn))
        self._populate_local_port_hct(port)
        wwn_buffer = self._extract_wwn_buffer_from_port_attributes(port_attributes)
        number_of_remote_ports = port_attributes.NumberOfDiscoveredPorts
        remote_ports = self._get_remote_ports(adapter_handle, port_index, number_of_remote_ports)
        port.discovered_ports = remote_ports
        port_statistics = self._get_local_port_statistics(adapter_handle, port_index, wwn_buffer)
        port.statistics = port_statistics
        port.all_port_mappings = self._get_local_port_mappings(adapter_handle, wwn_buffer)
        port_mappings = self._mappings_to_dict(port.all_port_mappings)
        for remote_port in port.discovered_ports:
            platform = get_platform_string()
            if "aix" in platform:
                channel, target = 0, int(remote_port.port_wwn._address, 16)
            else:
                channel, target = port_mappings.get(remote_port.port_wwn, (-1, -1))
            remote_port.hct = (port.hct[0], channel, target)
        return port

    def _get_local_port_statistics(self, adapter_handle, port_index, wwn_buffer):
        port_statistics = PortStatistics()
        hba_port_stats, hba_fc4_stats = None, None

        buff = ctypes.c_buffer(headers.HBA_PortStatistics.min_max_sizeof().max) #pylint: disable-msg=W0622,E1101
        try:
            c_api.HBA_GetPortStatistics(adapter_handle, port_index, buff)
        except RuntimeError:
            # Default QLogic drivers on Windows don't support this function - return defaults
            return port_statistics
        hba_port_stats = headers.HBA_PortStatistics.create_from_string(buff.raw) #pylint: disable-msg=E1101

        try:
            buff = ctypes.c_buffer(headers.HBA_FC4Statistics.min_max_sizeof().max) #pylint: disable-msg=W0622,E1101
            c_api.HBA_GetFC4Statistics(adapter_handle, wwn_buffer, 2, buff)
            hba_fc4_stats = headers.HBA_FC4Statistics.create_from_string(buff.raw) #pylint: disable-msg=E1101
        except NotImplementedError:
            pass
        except RuntimeError as exception:
            return_code = exception.args[0]
            log.error("HBA_GetFC4Statistics returned {0}".format(return_code))

        self._merge_statistics(port_statistics, hba_port_stats, hba_fc4_stats)
        return port_statistics

    def _merge_statistics(self, port_statistics, hba_port_stats=None, hba_fc4_stats=None):
        # FC_PORT_STATISTICS and HBA_PortStatistics and HBA_FC4Statistics are very similar
        def _merge_hba_port_stat(name):
            # some names are in different formatting
            alternative_name = name.replace('nos', 'NOS').replace('lip', 'LIP').replace('crc', 'CRC')
            hbaapi_name = ''.join([(item if item.isupper() else item.capitalize()) \
                                   for item in alternative_name.split('_')])
            setattr(port_statistics, name, -1 if hba_port_stats is None else getattr(hba_port_stats, hbaapi_name))

        def _merge_hba_fcp4_stat(name):
            hbaapi_name = ''.join([item.capitalize() for item in name.replace('fcp_', '').split('_')])
            setattr(port_statistics, name, -1 if hba_fc4_stats is None else getattr(hba_fc4_stats, hbaapi_name))

        for name in FC_PORT_STATISTICS:
            if name.startswith('fcp_'):
                _merge_hba_fcp4_stat(name)
            else:
                _merge_hba_port_stat(name)

    def _mappings_to_dict(self, mappings):
        def get_target_number(entry):
            platform = get_platform_string()
            if "solaris" in platform or "aix" in platform:
                return int(translate_wwn(entry.FcId.PortWWN)._address, 16)
            return entry.ScsiId.ScsiTargetNumber

        result = dict()
        for entry in mappings.entry:
            if entry.FcId.PortWWN == '':
                log.debug("Found an empty mapping")
                continue
            log.debug("Found mapping {!r} to {!r}".format(entry.ScsiId, entry.FcId.PortWWN))
            key = translate_wwn(entry.FcId.PortWWN)
            value = (entry.ScsiId.ScsiBusNumber, get_target_number(entry))
            result[key] = value
        log.debug("_mappings_to_dict result: {!r}".format(result))
        return result

    def _extract_wwn_buffer_from_port_attributes(self, port_attributes):
        wwn_string = b''.join([chr(item) if not PY3 else bytes([item]) for item in port_attributes.PortWWN])
        return ctypes.c_uint64(headers.UNInt64.create_from_string(wwn_string))

    def _build_empty_mappings_struct(self, number_of_elements):
        element_size = headers.HBA_FcpScsiEntryV2.min_max_sizeof().max
        entry = headers.HBA_FcpScsiEntryV2.create_from_string(b'\x00'*element_size)
        struct = headers.HBA_FCPTargetMappingV2(NumberOfEntries=number_of_elements,
                                                entry=[entry for index in range(number_of_elements)])
        return struct

    def _get_local_port_mappings_count(self, adapter_handle, wwn_buffer):
        struct = self._build_empty_mappings_struct(0)
        size = struct.sizeof(struct)
        buff = ctypes.c_buffer(b'\x00'*size, size)
        try:
            c_api.HBA_GetFcpTargetMappingV2(adapter_handle, wwn_buffer, buff)
        except RuntimeError as exception:
            return_code = exception.args[0]
            if return_code == headers.HBA_STATUS_ERROR_MORE_DATA:
                return headers.UNInt32.create_from_string(buff.raw)
        return 0

    def _get_local_port_mappings(self, adapter_handle, wwn_buffer):
        try:
            number_of_entries = self._get_local_port_mappings_count(adapter_handle, wwn_buffer)
            struct = self._build_empty_mappings_struct(number_of_entries)
            size = struct.sizeof(struct)
            buff = ctypes.c_buffer(struct.write_to_string(struct), size)
            c_api.HBA_GetFcpTargetMappingV2(adapter_handle, wwn_buffer, buff)
        except RuntimeError as exception:
            msg = "failed to fetch port mappings for local wwn {!r}"
            log.error(msg.format(binascii.hexlify(wwn_buffer)))
            return_code = exception.args[0]
            if return_code == headers.HBA_STATUS_ERROR_ILLEGAL_WWN:
                log.error("error code is HBA_STATUS_ERROR_ILLEGAL_WWN")
                return headers.HBA_FCPTargetMappingV2.create_empty()
            if return_code == headers.HBA_STATUS_ERROR_NOT_SUPPORTED:
                log.error("error code is HBA_STATUS_ERROR_NOT_SUPPORTED")
                return headers.HBA_FCPTargetMappingV2.create_empty()
            if return_code == headers.HBA_STATUS_ERROR_MORE_DATA:
                # NPIV ports seem to always return this
                return headers.HBA_FCPTargetMappingV2.create_empty()
            if return_code == headers.HBA_STATUS_ERROR:
                # HPT-1759 hyper-v windows throw error
                return headers.HBA_FCPTargetMappingV2.create_empty()
            else:
                raise

        mappings = headers.HBA_FCPTargetMappingV2.create_from_string(buff.raw)
        return mappings

    def _iter_ports(self):
        with self.hbaapi_library():
            for adapter_name in self._iter_adapters():
                with self.hbaapi_adapter(adapter_name) as adapter_handle:
                    try:
                        adapter_attributes = self._get_adapter_attributes(adapter_handle)
                    except NotImplementedError:
                        # because of weird HBAs, like VMWare's "LSI Adapter, SAS 3000 Series"
                        logging.debug("found a weird host bus adapter, named: %s", adapter_name.value)
                        continue
                    for port_index in range(0, adapter_attributes.NumberOfPorts):
                        log.debug("yield port index {} for adapter name {}".format(port_index, adapter_name))
                        local_port = self._get_local_port(adapter_handle, adapter_attributes, port_index)
                        yield local_port

    def iter_ports(self):
        for local_port in self._iter_ports():
            local_port.pop("all_port_mappings")
            yield local_port

    def iter_port_mappings(self):
        for local_port in self._iter_ports():
            for entry in local_port.all_port_mappings.entry:
                if entry.FcId.PortWWN == '':
                    log.debug("Found an empty mapping")
                    continue
                log.debug("Found mapping {!r} to {!r}".format(entry.ScsiId, entry.FcId.PortWWN))
                host_wwn = local_port.port_wwn
                target_wwn = translate_wwn(entry.FcId.PortWWN)
                device_name = entry.ScsiId.OSDeviceName.split("\x00")[0]
                lun = entry.ScsiId.ScsiOSLun
                yield device_name, host_wwn, target_wwn, lun

    @classmethod
    def is_available(cls):
        if get_platform_string().split('-')[0] not in ('windows', 'solaris', 'aix'):
            return False
        try:
            c_api.HBA_GetVersion._get_function() #pylint: disable-msg=W0212,E1101
            return True
        except OSError:
            pass
        return False

    @contextmanager
    def hbaapi_library(self):
        c_api.HBA_LoadLibrary()
        try:
            yield
        finally:
            c_api.HBA_FreeLibrary()

    @contextmanager
    def hbaapi_adapter(self, adapter_name):
        handle = c_api.HBA_OpenAdapter(adapter_name)
        try:
            yield handle
        finally:
            c_api.HBA_CloseAdapter(handle)

def translate_wwn(source):
    source = b''.join([chr(item) if not PY3 else bytes([item]) for item in source])
    return WWN(binascii.hexlify(source if source != '' else b'\x00'*8).decode("ascii"))

def translate_port_type(number):
    return headers.HBA_PORTTYPE[str(number)]

def translate_port_speed(source):
    """ PortSpeed indicates the signalling bit rate at which this port is currently operating.
    """
    translation = {
                   headers.HBA_PORTSPEED_UNKNOWN: 0,
                   headers.HBA_PORTSPEED_1GBIT: 1,
                   headers.HBA_PORTSPEED_2GBIT: 2,
                   headers.HBA_PORTSPEED_10GBIT: 10,
                   headers.HBA_PORTSPEED_4GBIT: 4,
                   headers.HBA_PORTSPEED_8GBIT: 8,
                   headers.HBA_PORTSPEED_16GBIT: 16,
                   headers.HBA_PORTSPEED_32GBIT: 32,
                   headers.HBA_PORTSPEED_20GBIT: 20,
                   headers.HBA_PORTSPEED_40GBIT: 40,
                   headers.HBA_PORTSPEED_NOT_NEGOTIATED: 0
                  }
    return translation.get(source, 0)

def translate_port_supported_speeds(source):
    """ PortSupportedSpeed indicates the signalling bit rates at which this port may operate.
    """
    result = []
    translation = {
                   headers.HBA_PORTSPEED_1GBIT: 1,
                   headers.HBA_PORTSPEED_2GBIT: 2,
                   headers.HBA_PORTSPEED_10GBIT: 10,
                   headers.HBA_PORTSPEED_4GBIT: 4,
                   headers.HBA_PORTSPEED_8GBIT: 8,
                   headers.HBA_PORTSPEED_16GBIT: 16,
                   headers.HBA_PORTSPEED_32GBIT: 32,
                   headers.HBA_PORTSPEED_20GBIT: 20,
                   headers.HBA_PORTSPEED_40GBIT: 40,
                   }
    keys = list(translation.keys())
    keys.sort(reverse=True)
    for key in keys:
        if key & source:
            result.append(translation[key])
            source -= key
    result.sort()
    return result

def translate_port_state(source):
    translation = {headers.HBA_PORTSTATE_UNKNOWN: None,
                   headers.HBA_PORTSTATE_ONLINE: 'online',
                   headers.HBA_PORTSTATE_OFFLINE: 'offline',
                   headers.HBA_PORTSTATE_BYPASSED: 'bypassed',
                   headers.HBA_PORTSTATE_DIAGNOSTICS: 'diagnostics',
                   headers.HBA_PORTSTATE_LINKDOWN: 'link down',
                   headers.HBA_PORTSTATE_ERROR: 'error',
                   headers.HBA_PORTSTATE_LOOPBACK: 'lookback'}
    return translation[source]

def get_port_object(adapter_attributes=Munch(), port_attributes=Munch()):
    kwargs = {}
    kwargs['node_wwn'] = translate_wwn(getattr(adapter_attributes, "NodeWWN", None) or \
                                       getattr(port_attributes, "NodeWWN", None))
    kwargs['port_wwn'] = translate_wwn(getattr(port_attributes, "PortWWN"))
    kwargs['port_fcid'] = getattr(port_attributes, "PortFcId")
    kwargs['port_type'] = translate_port_type(getattr(port_attributes, "PortType"))
    kwargs['port_state'] = translate_port_state(getattr(port_attributes, "PortState"))
    kwargs['port_speed'] = translate_port_speed(getattr(port_attributes, "PortSpeed"))
    kwargs['port_symbolic_name'] = getattr(port_attributes, "PortSymbolicName").strip(b'\x00')
    kwargs['os_device_name'] = getattr(port_attributes, "OSDeviceName").strip(b'\x00')
    kwargs['port_supported_speeds'] = translate_port_supported_speeds(getattr(port_attributes, "PortSuggestedSpeed"))
    kwargs['port_max_frame_size'] = getattr(port_attributes, "PortMaxFrameSize", None)
    kwargs['fabric_name'] = translate_wwn(getattr(port_attributes, "FabricName", None))
    kwargs['driver_name'] = getattr(adapter_attributes, "DriverName", b'').strip(b'\x00')
    kwargs['driver_version'] = getattr(adapter_attributes, "DriverVersion", b'').strip(b'\x00')
    kwargs['manufacturer'] = getattr(adapter_attributes, "Manufacturer", b'').strip(b'\x00')
    kwargs['serial_number'] = getattr(adapter_attributes, "SerialNumber", b'').strip(b'\x00')
    kwargs['model'] = getattr(adapter_attributes, "Model", b'').strip(b'\x00')
    kwargs['model_description'] = getattr(adapter_attributes, "ModelDescription", b'').strip(b'\x00')
    kwargs['hardware_version'] = getattr(adapter_attributes, "HardwareVersion", b'').strip(b'\x00')
    kwargs['firmware_version'] = getattr(adapter_attributes, "FirmwareVersion", b'').strip(b'\x00')
    kwargs['option_rom_version'] = getattr(adapter_attributes, "OptionROMVersion", b'').strip(b'\x00')
    port = Port()
    for key, value in kwargs.items():
        port[key] = value
    return port
