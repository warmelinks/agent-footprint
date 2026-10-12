import os
import unittest
from unittest import mock

from agent_footprint import watch
from agent_footprint.watch import Keyboard, parse_keys


class KeyTest(unittest.TestCase):
    def test_arrows(self):
        self.assertEqual(parse_keys("\x1b[A\x1b[B\x1b[C\x1b[D"), ["later", "earlier", "next", "prev"])
        self.assertEqual(parse_keys("\x1bOA\x1bOB"), ["later", "earlier"])  # application cursor mode

    def test_unknown_escape_sequences_are_not_read_as_keys(self):
        # F5, Home and Shift+Up contain '[' (earlier) and digits (jump), which must not fire.
        self.assertEqual(parse_keys("\x1b[15~\x1b[H\x1b[1;2A"), [])
        self.assertEqual(parse_keys("[]3q"), ["earlier", "later", 3, "quit"])

    @unittest.skipIf(os.name == "nt", "POSIX terminal input")
    def test_reads_a_posix_terminal(self):
        r, w = os.pipe()
        os.write(w, b"\x1b[Cq")
        try:
            kb = Keyboard()
            kb.fd = r
            self.assertEqual(kb.read(0.1), ["next", "quit"])
        finally:
            os.close(r), os.close(w)

    def test_windows_console_keys(self):
        typed = iter(["\xe0", "M", "\x00", "P", "q"])  # right arrow, down arrow (numpad), q
        fake = mock.Mock(kbhit=mock.Mock(side_effect=[True, True, True, True, False]),
                         getwch=mock.Mock(side_effect=lambda: next(typed)))
        with mock.patch.object(watch, "msvcrt", fake, create=True), mock.patch.object(watch.os, "name", "nt"):
            kb = Keyboard()
            kb.fd = 0
            self.assertEqual(kb.read(0.1), ["next", "earlier", "quit"])
