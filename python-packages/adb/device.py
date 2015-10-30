#
# Copyright (C) 2015 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import atexit
import contextlib
import logging
import os
import re
import subprocess
import tempfile


class FindDeviceError(RuntimeError):
    pass


class DeviceNotFoundError(FindDeviceError):
    def __init__(self, serial):
        self.serial = serial
        super(DeviceNotFoundError, self).__init__(
            'No device with serial {}'.format(serial))


class NoUniqueDeviceError(FindDeviceError):
    def __init__(self):
        super(NoUniqueDeviceError, self).__init__('No unique device')


class ShellError(RuntimeError):
    def __init__(self, cmd, stdout, stderr, exit_code):
        super(ShellError, self).__init__(
                '`{0}` exited with code {1}'.format(cmd, exit_code))
        self.cmd = cmd
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


def get_devices(adb_path='adb'):
    with open(os.devnull, 'wb') as devnull:
        subprocess.check_call([adb_path, 'start-server'], stdout=devnull,
                              stderr=devnull)
    out = subprocess.check_output([adb_path, 'devices']).splitlines()

    # The first line of `adb devices` just says "List of attached devices", so
    # skip that.
    devices = []
    for line in out[1:]:
        if not line.strip():
            continue
        if 'offline' in line:
            continue

        serial, _ = re.split(r'\s+', line, maxsplit=1)
        devices.append(serial)
    return devices


def _get_unique_device(product=None, adb_path='adb'):
    devices = get_devices(adb_path=adb_path)
    if len(devices) != 1:
        raise NoUniqueDeviceError()
    return AndroidDevice(devices[0], product, adb_path)


def _get_device_by_serial(serial, product=None, adb_path='adb'):
    for device in get_devices(adb_path=adb_path):
        if device == serial:
            return AndroidDevice(serial, product, adb_path)
    raise DeviceNotFoundError(serial)


def get_device(serial=None, product=None, adb_path='adb'):
    """Get a uniquely identified AndroidDevice if one is available.

    Raises:
        DeviceNotFoundError:
            The serial specified by `serial` or $ANDROID_SERIAL is not
            connected.

        NoUniqueDeviceError:
            Neither `serial` nor $ANDROID_SERIAL was set, and the number of
            devices connected to the system is not 1. Having 0 connected
            devices will also result in this error.

    Returns:
        An AndroidDevice associated with the first non-None identifier in the
        following order of preference:

        1) The `serial` argument.
        2) The environment variable $ANDROID_SERIAL.
        3) The single device connnected to the system.
    """
    if serial is not None:
        return _get_device_by_serial(serial, product, adb_path)

    android_serial = os.getenv('ANDROID_SERIAL')
    if android_serial is not None:
        return _get_device_by_serial(android_serial, product, adb_path)

    return _get_unique_device(product, adb_path=adb_path)


def _get_device_by_type(flag, adb_path):
    with open(os.devnull, 'wb') as devnull:
        subprocess.check_call([adb_path, 'start-server'], stdout=devnull,
                              stderr=devnull)
    try:
        serial = subprocess.check_output([adb_path, flag, 'get-serialno']).strip()
    except subprocess.CalledProcessError:
        raise RuntimeError('adb unexpectedly returned nonzero')
    if serial == 'unknown':
        raise NoUniqueDeviceError()
    return _get_device_by_serial(serial, adb_path=adb_path)


def get_usb_device(adb_path='adb'):
    """Get the unique USB-connected AndroidDevice if it is available.

    Raises:
        NoUniqueDeviceError:
            0 or multiple devices are connected via USB.

    Returns:
        An AndroidDevice associated with the unique USB-connected device.
    """
    return _get_device_by_type('-d', adb_path=adb_path)


def get_emulator_device(adb_path='adb'):
    """Get the unique emulator AndroidDevice if it is available.

    Raises:
        NoUniqueDeviceError:
            0 or multiple emulators are running.

    Returns:
        An AndroidDevice associated with the unique running emulator.
    """
    return _get_device_by_type('-e', adb_path=adb_path)


@contextlib.contextmanager
def _file_deleter(f):
    yield
    if f:
        f.close()
        os.remove(f.name)


# Internal helper that may return a temporary file (containing a command line
# in UTF-8) that should be executed with the help of _get_subprocess_args().
def _get_windows_unicode_helper(args):
    # Only do this slow work-around if Unicode is in the cmd line on Windows.
    if (os.name != 'nt' or all(not isinstance(arg, unicode) for arg in args)):
        return None

    # cmd.exe requires a suffix to know that it is running a batch file.
    # We can't use delete=True because that causes File Share Mode Delete to be
    # used which prevents the file from being opened by other processes that
    # don't use that File Share Mode. The caller must manually delete the file.
    tf = tempfile.NamedTemporaryFile('wb', suffix='.cmd', delete=False)
    # @ in batch suppresses echo of the current line.
    # Change the codepage to 65001, the UTF-8 codepage.
    tf.write('@chcp 65001 > nul\r\n')
    tf.write('@')
    # Properly quote all the arguments and encode in UTF-8.
    tf.write(subprocess.list2cmdline(args).encode('utf-8'))
    tf.close()
    return tf


# Let the caller know how to run the batch file. Takes subprocess.check_output()
# or subprocess.Popen() args and returns a new tuple that should be passed
# instead, or the original args if there is no file
def _get_subprocess_args(args, helper_file):
    if helper_file:
        # Concatenate our new command line args with any other function args.
        return (['cmd.exe', '/c', helper_file.name],) + args[1:]
    else:
        return args


# Call this instead of subprocess.check_output() to work-around issue in Python
# 2's subprocess class on Windows where it doesn't support Unicode. This
# writes the command line to a UTF-8 batch file that is properly interpreted
# by cmd.exe.
def _subprocess_check_output(*args, **kwargs):
    helper = _get_windows_unicode_helper(args[0])
    with _file_deleter(helper):
        try:
            return subprocess.check_output(
                    *_get_subprocess_args(args, helper), **kwargs)
        except subprocess.CalledProcessError as e:
            # Show real command line instead of the cmd.exe command line.
            raise subprocess.CalledProcessError(e.returncode, args[0],
                                                output=e.output)


# Call this instead of subprocess.Popen(). Like _subprocess_check_output().
class _subprocess_Popen(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        # __del__() can be called after global teardown has started, meaning
        # the global references to _subprocess_Popen and the os module may
        # no longer exist. We need to save local references to all global names
        # used in __del__() to avoid this.
        self.saved_class = _subprocess_Popen
        self.saved_os = os
        # Save reference to helper so that it can be deleted once it is no
        # longer used.
        self.helper = _get_windows_unicode_helper(args[0])
        super(_subprocess_Popen, self).__init__(
                *_get_subprocess_args(args, self.helper), **kwargs)

    def __del__(self, *args, **kwargs):
        super(self.saved_class, self).__del__(*args, **kwargs)
        if self.helper:
            self.saved_os.remove(self.helper.name)


class AndroidDevice(object):
    # Delimiter string to indicate the start of the exit code.
    _RETURN_CODE_DELIMITER = 'x'

    # Follow any shell command with this string to get the exit
    # status of a program since this isn't propagated by adb.
    #
    # The delimiter is needed because `printf 1; echo $?` would print
    # "10", and we wouldn't be able to distinguish the exit code.
    _RETURN_CODE_PROBE_STRING = 'echo "{0}$?"'.format(_RETURN_CODE_DELIMITER)

    # Maximum search distance from the output end to find the delimiter.
    # adb on Windows returns \r\n even if adbd returns \n.
    _RETURN_CODE_SEARCH_LENGTH = len('{0}255\r\n'.format(_RETURN_CODE_DELIMITER))

    # Feature name strings.
    SHELL_PROTOCOL_FEATURE = 'shell_v2'

    def __init__(self, serial, product=None, adb_path='adb'):
        self.serial = serial
        self.product = product
        self.adb_cmd = [adb_path]

        if self.serial is not None:
            self.adb_cmd.extend(['-s', serial])
        if self.product is not None:
            self.adb_cmd.extend(['-p', product])
        self._linesep = None
        self._features = None

    @property
    def linesep(self):
        if self._linesep is None:
            self._linesep = subprocess.check_output(self.adb_cmd +
                                                    ['shell', 'echo'])
        return self._linesep

    @property
    def features(self):
        if self._features is None:
            try:
                self._features = self._simple_call(['features']).splitlines()
            except subprocess.CalledProcessError:
                self._features = []
        return self._features

    def _make_shell_cmd(self, user_cmd):
        command = self.adb_cmd + ['shell'] + user_cmd
        if self.SHELL_PROTOCOL_FEATURE not in self.features:
            command.append('; ' + self._RETURN_CODE_PROBE_STRING)
        return command

    def _parse_shell_output(self, out):
        """Finds the exit code string from shell output.

        Args:
            out: Shell output string.

        Returns:
            An (exit_code, output_string) tuple. The output string is
            cleaned of any additional stuff we appended to find the
            exit code.

        Raises:
            RuntimeError: Could not find the exit code in |out|.
        """
        search_text = out
        if len(search_text) > self._RETURN_CODE_SEARCH_LENGTH:
            # We don't want to search over massive amounts of data when we know
            # the part we want is right at the end.
            search_text = search_text[-self._RETURN_CODE_SEARCH_LENGTH:]
        partition = search_text.rpartition(self._RETURN_CODE_DELIMITER)
        if partition[1] == '':
            raise RuntimeError('Could not find exit status in shell output.')
        result = int(partition[2])
        # partition[0] won't contain the full text if search_text was truncated,
        # pull from the original string instead.
        out = out[:-len(partition[1]) - len(partition[2])]
        return result, out

    def _simple_call(self, cmd):
        logging.info(' '.join(self.adb_cmd + cmd))
        return _subprocess_check_output(
            self.adb_cmd + cmd, stderr=subprocess.STDOUT)

    def shell(self, cmd):
        """Calls `adb shell`

        Args:
            cmd: command to execute as a list of strings.

        Returns:
            A (stdout, stderr) tuple. Stderr may be combined into stdout
            if the device doesn't support separate streams.

        Raises:
            ShellError: the exit code was non-zero.
        """
        exit_code, stdout, stderr = self.shell_nocheck(cmd)
        if exit_code != 0:
            raise ShellError(cmd, stdout, stderr, exit_code)
        return stdout, stderr

    def shell_nocheck(self, cmd):
        """Calls `adb shell`

        Args:
            cmd: command to execute as a list of strings.

        Returns:
            An (exit_code, stdout, stderr) tuple. Stderr may be combined
            into stdout if the device doesn't support separate streams.
        """
        cmd = self._make_shell_cmd(cmd)
        logging.info(' '.join(cmd))
        p = _subprocess_Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = p.communicate()
        if self.SHELL_PROTOCOL_FEATURE in self.features:
            exit_code = p.returncode
        else:
            exit_code, stdout = self._parse_shell_output(stdout)
        return exit_code, stdout, stderr

    def shell_popen(self, cmd, kill_atexit=True, preexec_fn=None,
                    creationflags=0, **kwargs):
        """Calls `adb shell` and returns a handle to the adb process.

        This function provides direct access to the subprocess used to run the
        command, without special return code handling. Users that need the
        return value must retrieve it themselves.

        Args:
            cmd: Array of command arguments to execute.
            kill_atexit: Whether to kill the process upon exiting.
            preexec_fn: Argument forwarded to subprocess.Popen.
            creationflags: Argument forwarded to subprocess.Popen.
            **kwargs: Arguments forwarded to subprocess.Popen.

        Returns:
            subprocess.Popen handle to the adb shell instance
        """

        command = self.adb_cmd + ['shell'] + cmd

        # Make sure a ctrl-c in the parent script doesn't kill gdbserver.
        if os.name == 'nt':
            creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            if preexec_fn is None:
                preexec_fn = os.setpgrp
            elif preexec_fn is not os.setpgrp:
                fn = preexec_fn
                def _wrapper():
                    fn()
                    os.setpgrp()
                preexec_fn = _wrapper

        p = _subprocess_Popen(command, creationflags=creationflags,
                              preexec_fn=preexec_fn, **kwargs)

        if kill_atexit:
            atexit.register(p.kill)

        return p

    def install(self, filename, replace=False):
        cmd = ['install']
        if replace:
            cmd.append('-r')
        cmd.append(filename)
        return self._simple_call(cmd)

    def push(self, local, remote):
        return self._simple_call(['push', local, remote])

    def pull(self, remote, local):
        return self._simple_call(['pull', remote, local])

    def sync(self, directory=None):
        cmd = ['sync']
        if directory is not None:
            cmd.append(directory)
        return self._simple_call(cmd)

    def forward(self, local, remote):
        return self._simple_call(['forward', local, remote])

    def tcpip(self, port):
        return self._simple_call(['tcpip', port])

    def usb(self):
        return self._simple_call(['usb'])

    def reboot(self):
        return self._simple_call(['reboot'])

    def remount(self):
        return self._simple_call(['remount'])

    def root(self):
        return self._simple_call(['root'])

    def unroot(self):
        return self._simple_call(['unroot'])

    def forward_remove(self, local):
        return self._simple_call(['forward', '--remove', local])

    def forward_remove_all(self):
        return self._simple_call(['forward', '--remove-all'])

    def connect(self, host):
        return self._simple_call(['connect', host])

    def disconnect(self, host):
        return self._simple_call(['disconnect', host])

    def reverse(self, remote, local):
        return self._simple_call(['reverse', remote, local])

    def reverse_remove_all(self):
        return self._simple_call(['reverse', '--remove-all'])

    def reverse_remove(self, remote):
        return self._simple_call(['reverse', '--remove', remote])

    def wait(self):
        return self._simple_call(['wait-for-device'])

    def get_props(self):
        result = {}
        output, _ = self.shell(['getprop'])
        output = output.splitlines()
        pattern = re.compile(r'^\[([^]]+)\]: \[(.*)\]')
        for line in output:
            match = pattern.match(line)
            if match is None:
                raise RuntimeError('invalid getprop line: "{}"'.format(line))
            key = match.group(1)
            value = match.group(2)
            if key in result:
                raise RuntimeError('duplicate getprop key: "{}"'.format(key))
            result[key] = value
        return result

    def get_prop(self, prop_name):
        output = self.shell(['getprop', prop_name])[0].splitlines()
        if len(output) != 1:
            raise RuntimeError('Too many lines in getprop output:\n' +
                               '\n'.join(output))
        value = output[0]
        if not value.strip():
            return None
        return value

    def set_prop(self, prop_name, value):
        self.shell(['setprop', prop_name, value])
