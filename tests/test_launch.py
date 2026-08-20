"""Unit tests for the start.command launcher helpers."""
import os
import sys
import unittest
from unittest.mock import patch

import launch


class TestCloseLauncherWindow(unittest.TestCase):
    def test_should_close_only_for_finder_mac_terminal(self):
        env = {
            'SACN2HOMELX_CLOSE_WINDOW': '1',
            'TERM_PROGRAM': 'Apple_Terminal',
        }
        with patch.object(sys, 'platform', 'darwin'), patch.dict(os.environ, env, clear=True):
            self.assertTrue(launch._should_close_launcher_window())

    def test_should_not_close_without_flag(self):
        env = {'TERM_PROGRAM': 'Apple_Terminal'}
        with patch.object(sys, 'platform', 'darwin'), patch.dict(os.environ, env, clear=True):
            self.assertFalse(launch._should_close_launcher_window())

    def test_should_not_close_from_ssh_or_other_os(self):
        env = {
            'SACN2HOMELX_CLOSE_WINDOW': '1',
            'TERM_PROGRAM': 'Apple_Terminal',
            'SSH_TTY': '/dev/ttys001',
        }
        with patch.object(sys, 'platform', 'darwin'), patch.dict(os.environ, env, clear=True):
            self.assertFalse(launch._should_close_launcher_window())
        env.pop('SSH_TTY')
        with patch.object(sys, 'platform', 'linux'), patch.dict(os.environ, env, clear=True):
            self.assertFalse(launch._should_close_launcher_window())

    def test_should_close_windows_start_bat(self):
        env = {'SACN2HOMELX_CLOSE_WINDOW': '1'}
        with patch.object(sys, 'platform', 'win32'), patch.dict(os.environ, env, clear=True):
            self.assertTrue(launch._should_close_launcher_window())
        with patch.object(sys, 'platform', 'win32'), patch.dict(os.environ, {}, clear=True):
            self.assertFalse(launch._should_close_launcher_window())

    def test_ctrl_c_is_a_clean_quit(self):
        self.assertTrue(launch._is_clean_quit(0))
        self.assertTrue(launch._is_clean_quit(130))
        self.assertTrue(launch._is_clean_quit(-2))
        self.assertTrue(launch._is_clean_quit(0xC000013A))
        self.assertFalse(launch._is_clean_quit(1))
