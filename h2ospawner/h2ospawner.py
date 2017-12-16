"""
Contains H2O Spawner class & default implementation
"""

# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.

import errno
import os
import pipes
import shutil
import signal
import sys
import warnings
from subprocess import Popen
from tempfile import mkdtemp

from sqlalchemy import inspect

from tornado import gen
from tornado.ioloop import PeriodicCallback

from traitlets.config import LoggingConfigurable
from traitlets import (
    Any, Bool, Dict, Instance, Integer, Float, List, Unicode,
    observe, validate,
)

#from jupyterhub.objects import Server
from jupyterhub.traitlets import Command
from jupyterhub.utils import random_port   #, url_path_join, exponential_backoff

from jupyterhub.spawner import Spawner


def _try_setcwd(path):
    """Try to set CWD to path, walking up until a valid directory is found.

    If no valid directory is found, a temp directory is created and cwd is set to that.
    """
    while path != '/':
        try:
            os.chdir(path)
        except OSError as e:
            exc = e  # break exception instance out of except scope
            print("Couldn't set CWD to %s (%s)" % (path, e), file=sys.stderr)
            path, _ = os.path.split(path)
        else:
            return
    print("Couldn't set CWD at all (%s), using temp dir" % exc, file=sys.stderr)
    td = mkdtemp()
    os.chdir(td)


def set_user_setuid(username, chdir=True):
    """Return a preexec_fn for spawning a single-user server as a particular user.

    Returned preexec_fn will set uid/gid, and attempt to chdir to the target user's
    home directory.
    """
    import grp
    import pwd
    user = pwd.getpwnam(username)
    uid = user.pw_uid
    gid = user.pw_gid
    home = user.pw_dir
    gids = [ g.gr_gid for g in grp.getgrall() if username in g.gr_mem ]

    def preexec():
        """Set uid/gid of current process

        Executed after fork but before exec by python.

        Also try to chdir to the user's home directory.
        """
        os.setgid(gid)
        try:
            os.setgroups(gids)
        except Exception as e:
            print('Failed to set groups %s' % e, file=sys.stderr)
        os.setuid(uid)

        # start in the user's home dir
        if chdir:
            _try_setcwd(home)

    return preexec



class H2OSpawner(Spawner):
    """
    A Spawner that uses `subprocess.Popen` to start single-user servers as local processes.

    Requires local UNIX users matching the authenticated users to exist.
    Does not work on Windows.

    This is the default spawner for JupyterHub.
    """

    interrupt_timeout = Integer(10,
        help="""
        Seconds to wait for single-user server process to halt after SIGINT.

        If the process has not exited cleanly after this many seconds, a SIGTERM is sent.
        """
    ).tag(config=True)

    term_timeout = Integer(5,
        help="""
        Seconds to wait for single-user server process to halt after SIGTERM.

        If the process does not exit cleanly after this many seconds of SIGTERM, a SIGKILL is sent.
        """
    ).tag(config=True)

    kill_timeout = Integer(5,
        help="""
        Seconds to wait for process to halt after SIGKILL before giving up.

        If the process does not exit cleanly after this many seconds of SIGKILL, it becomes a zombie
        process. The hub process will log a warning and then give up.
        """
    ).tag(config=True)

    popen_kwargs = Dict(
        help="""Extra keyword arguments to pass to Popen

        when spawning single-user servers.

        For example::

            popen_kwargs = dict(shell=True)

        """
    ).tag(config=True)
    shell_cmd = Command(minlen=0,
        help="""Specify a shell command to launch.

        The single-user command will be appended to this list,
        so it sould end with `-c` (for bash) or equivalent.

        For example::

            c.H2OSpawner.shell_cmd = ['bash', '-l', '-c']

        to launch with a bash login shell, which would set up the user's own complete environment.

        .. warning::

            Using shell_cmd gives users control over PATH, etc.,
            which could change what the jupyterhub-singleuser launch command does.
            Only use this for trusted users.
        """
    )

    proc = Instance(Popen,
        allow_none=True,
        help="""
        The process representing the single-user server process spawned for current user.

        Is None if no process has been spawned yet.
        """)
    pid = Integer(0,
        help="""
        The process id (pid) of the single-user server process spawned for current user.
        """
    )

    h2ocmd = Command(['java', '-jar', '/Users/c2j/Projects/h2oai/h2o-3.16.0.2/h2o.jar'],
        allow_none=True,
        help="""
        The command used for starting the single-user server.

        Provide either a string or a list containing the path to the startup script command. Extra arguments,
        other than this path, should be provided via `args`.

        This is usually set if you want to start the single-user server in a different python
        environment (with virtualenv/conda) than JupyterHub itself.

        Some spawners allow shell-style expansion here, allowing you to use environment variables.
        Most, including the default, do not. Consult the documentation for your spawner to verify!
        """
    ).tag(config=True)


    def make_preexec_fn(self, name):
        """
        Return a function that can be used to set the user id of the spawned process to user with name `name`

        This function can be safely passed to `preexec_fn` of `Popen`
        """
        return set_user_setuid(name)

    def load_state(self, state):
        """Restore state about spawned single-user server after a hub restart.

        Local processes only need the process id.
        """
        super(H2OSpawner, self).load_state(state)
        if 'pid' in state:
            self.pid = state['pid']

    def get_state(self):
        """Save state that is needed to restore this spawner instance after a hub restore.

        Local processes only need the process id.
        """
        state = super(H2OSpawner, self).get_state()
        if self.pid:
            state['pid'] = self.pid
        return state

    def clear_state(self):
        """Clear stored state about this spawner (pid)"""
        super(H2OSpawner, self).clear_state()
        self.pid = 0

    def user_env(self, env):
        """Augment environment of spawned process with user specific env variables."""
        import pwd
        env['USER'] = self.user.name
        home = pwd.getpwnam(self.user.name).pw_dir
        shell = pwd.getpwnam(self.user.name).pw_shell
        # These will be empty if undefined,
        # in which case don't set the env:
        if home:
            env['HOME'] = home
        if shell:
            env['SHELL'] = shell
        return env

    def get_env(self):
        """Get the complete set of environment variables to be set in the spawned process."""
        env = super().get_env()
        env = self.user_env(env)
        return env

    def get_h2oargs(self):
        """Return the arguments to be passed after self.cmd

        Doesn't expect shell expansion to happen.
        """
        args = []

        if self.ip:
            args.append('-ip "%s"' % self.ip)

        if self.port:
            args.append('-port')
            args.append('%i' % self.port)
        elif self.server.port:
            self.log.warning("Setting port from user.server is deprecated as of JupyterHub 0.7.")
            args.append('-port %i' % self.server.port)

        if self.notebook_dir:
            notebook_dir = self.format_string(self.notebook_dir)
            args.append('--notebook-dir="%s"' % notebook_dir)
        if self.default_url:
            default_url = self.format_string(self.default_url)
            args.append('--NotebookApp.default_url="%s"' % default_url)

        if self.debug:
            args.append('--debug')
        if self.disable_user_config:
            args.append('--disable-user-config')
        args.append('-context_path')
        args.append('/user/%s' % self.user.name)
        args.extend(self.args)
        return args

    @gen.coroutine
    def start(self):
        """Start the single-user server."""
        self.port = random_port()
        cmd = []
        env = self.get_env()

        cmd.extend(self.h2ocmd)
        cmd.extend(self.get_h2oargs())

        if self.shell_cmd:
            # using shell_cmd (e.g. bash -c),
            # add our cmd list as the last (single) argument:
            cmd = self.shell_cmd + [' '.join(pipes.quote(s) for s in cmd)]

        self.log.info("Spawning %s", ' '.join(pipes.quote(s) for s in cmd))

        popen_kwargs = dict(
            preexec_fn=self.make_preexec_fn(self.user.name),
            start_new_session=True,  # don't forward signals
        )
        popen_kwargs.update(self.popen_kwargs)
        # don't let user config override env
        popen_kwargs['env'] = env
        try:
            self.proc = Popen(cmd, **popen_kwargs)
        except PermissionError:
            # use which to get abspath
            script = shutil.which(cmd[0]) or cmd[0]
            self.log.error("Permission denied trying to run %r. Does %s have access to this file?",
                script, self.user.name,
            )
            raise

        self.pid = self.proc.pid

        if self.__class__ is not H2OSpawner:
            # subclasses may not pass through return value of super().start,
            # relying on deprecated 0.6 way of setting ip, port,
            # so keep a redundant copy here for now.
            # A deprecation warning will be shown if the subclass
            # does not return ip, port.
            if self.ip:
                self.server.ip = self.ip
            self.server.port = self.port
        return (self.ip or '127.0.0.1', self.port)

    @gen.coroutine
    def poll(self):
        """Poll the spawned process to see if it is still running.

        If the process is still running, we return None. If it is not running,
        we return the exit code of the process if we have access to it, or 0 otherwise.
        """
        # if we started the process, poll with Popen
        if self.proc is not None:
            status = self.proc.poll()
            if status is not None:
                # clear state if the process is done
                self.clear_state()
            return status

        # if we resumed from stored state,
        # we don't have the Popen handle anymore, so rely on self.pid
        if not self.pid:
            # no pid, not running
            self.clear_state()
            return 0

        # send signal 0 to check if PID exists
        # this doesn't work on Windows, but that's okay because we don't support Windows.
        alive = yield self._signal(0)
        if not alive:
            self.clear_state()
            return 0
        else:
            return None

    @gen.coroutine
    def _signal(self, sig):
        """Send given signal to a single-user server's process.

        Returns True if the process still exists, False otherwise.

        The hub process is assumed to have enough privileges to do this (e.g. root).
        """
        try:
            os.kill(self.pid, sig)
        except OSError as e:
            if e.errno == errno.ESRCH:
                return False  # process is gone
            else:
                raise
        return True  # process exists

    @gen.coroutine
    def stop(self, now=False):
        """Stop the single-user server process for the current user.

        If `now` is False (default), shutdown the server as gracefully as possible,
        e.g. starting with SIGINT, then SIGTERM, then SIGKILL.
        If `now` is True, terminate the server immediately.

        The coroutine should return when the process is no longer running.
        """
        if not now:
            status = yield self.poll()
            if status is not None:
                return
            self.log.debug("Interrupting %i", self.pid)
            yield self._signal(signal.SIGINT)
            yield self.wait_for_death(self.interrupt_timeout)

        # clean shutdown failed, use TERM
        status = yield self.poll()
        if status is not None:
            return
        self.log.debug("Terminating %i", self.pid)
        yield self._signal(signal.SIGTERM)
        yield self.wait_for_death(self.term_timeout)

        # TERM failed, use KILL
        status = yield self.poll()
        if status is not None:
            return
        self.log.debug("Killing %i", self.pid)
        yield self._signal(signal.SIGKILL)
        yield self.wait_for_death(self.kill_timeout)

        status = yield self.poll()
        if status is None:
            # it all failed, zombie process
            self.log.warning("Process %i never died", self.pid)
