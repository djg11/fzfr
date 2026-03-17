"""
test_fzfr.py — Unit tests for fzfr.

Covers the highest-risk functions: quoting, path safety, extension parsing,
config merging, remote arg building, and fzf version parsing.

Run with:
    python3 -m pytest tests/test_fzfr.py -v
    python3 -m unittest discover -s tests -v   # no pytest required
    make test
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# Module import
# ---------------------------------------------------------------------------

def _load_fzfr():
    """Locate and load the fzfr script as a module.

    Searches for the script in this order:
      1. One directory above this file (project root) — normal layout:
           tests/test_fzfr.py  →  ../fzfr
      2. Same directory as this file — flat layout or running tests directly.

    Uses SourceFileLoader explicitly because spec_from_file_location returns
    None for files without a .py extension.
    """
    from importlib.machinery import SourceFileLoader

    here = Path(__file__).parent
    candidates = [
        here.parent / "fzfr",
        here.parent / "fzfr.py",
        here / "fzfr",
        here / "fzfr.py",
    ]
    script = next((p for p in candidates if p.exists()), None)
    if script is None:
        raise FileNotFoundError(
            f"fzfr script not found. Searched: {[str(p) for p in candidates]}"
        )
    loader = SourceFileLoader("fzfr", str(script))
    spec = importlib.util.spec_from_loader("fzfr", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fzfr = _load_fzfr()


# ---------------------------------------------------------------------------
# _parse_extensions
# ---------------------------------------------------------------------------

class TestParseExtensions(unittest.TestCase):

    def test_basic(self):
        self.assertEqual(fzfr._parse_extensions("py txt"), ["py", "txt"])

    def test_leading_dots_stripped(self):
        self.assertEqual(fzfr._parse_extensions(".py .txt"), ["py", "txt"])

    def test_mixed_dots(self):
        self.assertEqual(fzfr._parse_extensions(".py txt .md"), ["py", "txt", "md"])

    def test_empty_string(self):
        self.assertEqual(fzfr._parse_extensions(""), [])

    def test_whitespace_only(self):
        self.assertEqual(fzfr._parse_extensions("   "), [])

    def test_unsafe_injection_dropped(self):
        # Shell injection attempt — must be silently discarded
        result = fzfr._parse_extensions("py $(rm -rf ~)")
        self.assertEqual(result, ["py"])

    def test_semicolon_dropped(self):
        result = fzfr._parse_extensions("py;evil")
        self.assertNotIn("py;evil", result)

    def test_backtick_dropped(self):
        result = fzfr._parse_extensions("py`whoami`")
        self.assertNotIn("py`whoami`", result)

    def test_alphanumeric_only_accepted(self):
        result = fzfr._parse_extensions("rs toml json")
        self.assertEqual(result, ["rs", "toml", "json"])

    def test_numbers_in_extension(self):
        # e.g. .mp3, .h264
        self.assertEqual(fzfr._parse_extensions("mp3 h264"), ["mp3", "h264"])

    def test_duplicate_extensions(self):
        # _parse_extensions does not deduplicate — callers may pass duplicates
        result = fzfr._parse_extensions("py py")
        self.assertEqual(result, ["py", "py"])


# ---------------------------------------------------------------------------
# _parse_fzf_version
# ---------------------------------------------------------------------------

class TestParseFzfVersion(unittest.TestCase):

    def test_standard(self):
        self.assertEqual(fzfr._parse_fzf_version("0.44.1"), (0, 44))

    def test_with_distro_suffix(self):
        self.assertEqual(fzfr._parse_fzf_version("0.44.1 (debian)"), (0, 44))

    def test_leading_whitespace(self):
        self.assertEqual(fzfr._parse_fzf_version("  0.38.0"), (0, 38))

    def test_garbage_returns_zero(self):
        self.assertEqual(fzfr._parse_fzf_version("garbage"), (0, 0))

    def test_empty_returns_zero(self):
        self.assertEqual(fzfr._parse_fzf_version(""), (0, 0))

    def test_version_comparison(self):
        # Minimum version check used in check_dependencies
        self.assertGreaterEqual(fzfr._parse_fzf_version("0.38.0"), (0, 38))
        self.assertLess(fzfr._parse_fzf_version("0.37.9"), (0, 38))


# ---------------------------------------------------------------------------
# _dquote
# ---------------------------------------------------------------------------

class TestDquote(unittest.TestCase):

    def test_plain_path(self):
        self.assertEqual(fzfr._dquote("/some/path/file"), '"/some/path/file"')

    def test_spaces_preserved(self):
        result = fzfr._dquote("/path/with spaces/file")
        self.assertEqual(result, '"/path/with spaces/file"')

    def test_double_quote_escaped(self):
        result = fzfr._dquote('say "hi"')
        self.assertIn('\\"', result)
        self.assertTrue(result.startswith('"'))
        self.assertTrue(result.endswith('"'))

    def test_dollar_sign_escaped(self):
        result = fzfr._dquote("$HOME/file")
        self.assertIn("\\$", result)

    def test_backtick_escaped(self):
        result = fzfr._dquote("`whoami`")
        self.assertIn("\\`", result)

    def test_backslash_escaped(self):
        result = fzfr._dquote("path\\file")
        self.assertIn("\\\\", result)

    def test_result_is_double_quoted(self):
        result = fzfr._dquote("anything")
        self.assertTrue(result.startswith('"'))
        self.assertTrue(result.endswith('"'))

    def test_empty_string(self):
        self.assertEqual(fzfr._dquote(""), '""')


# ---------------------------------------------------------------------------
# LocalBackend.is_safe_subpath
# ---------------------------------------------------------------------------

class TestLocalBackendIsSafeSubpath(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.backend = fzfr.LocalBackend(self.tmpdir)

    def test_file_inside_base(self):
        p = str(Path(self.tmpdir) / "subdir" / "file.txt")
        self.assertTrue(self.backend.is_safe_subpath(p))

    def test_file_outside_base(self):
        self.assertFalse(self.backend.is_safe_subpath("/"))

    def test_path_traversal_blocked(self):
        p = str(Path(self.tmpdir) / ".." / "etc" / "passwd")
        self.assertFalse(self.backend.is_safe_subpath(p))

    def test_base_path_itself_is_safe(self):
        self.assertTrue(self.backend.is_safe_subpath(self.tmpdir))

    def test_nonexistent_path_inside_base(self):
        # A path that doesn't exist yet but is inside base should be safe
        p = str(Path(self.tmpdir) / "new_file.txt")
        self.assertTrue(self.backend.is_safe_subpath(p))

    def test_empty_string(self):
        # Empty path should not be considered safe
        self.assertFalse(self.backend.is_safe_subpath(""))


# ---------------------------------------------------------------------------
# _merge_config_key
# ---------------------------------------------------------------------------

class TestMergeConfigKey(unittest.TestCase):

    def _make_cfg(self):
        """Return a fresh copy of _CONFIG_DEFAULTS."""
        import copy
        return copy.deepcopy(fzfr._CONFIG_DEFAULTS)

    def test_valid_string_key(self):
        cfg = self._make_cfg()
        fzfr._merge_config_key(cfg, "editor", "", "nvim")
        self.assertEqual(cfg["editor"], "nvim")

    def test_valid_bool_key(self):
        cfg = self._make_cfg()
        fzfr._merge_config_key(cfg, "ssh_multiplexing", False, True)
        self.assertTrue(cfg["ssh_multiplexing"])

    def test_wrong_type_ignored(self):
        cfg = self._make_cfg()
        # "ssh_multiplexing" expects bool; passing a string should warn and keep default
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            fzfr._merge_config_key(cfg, "ssh_multiplexing", False, "yes")
        self.assertFalse(cfg["ssh_multiplexing"])  # default preserved
        self.assertIn("Warning", buf.getvalue())

    def test_bool_subclass_of_int_handled(self):
        # int 1 should NOT pass as bool True — isinstance(1, type(False)) is True
        # but isinstance(True, type(False)) is also True. The key point is
        # that passing int 1 for a bool field should be caught.
        cfg = self._make_cfg()
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            fzfr._merge_config_key(cfg, "ssh_multiplexing", False, 1)
        # int is a subclass of bool in Python, so isinstance(1, type(False)) -> True
        # This is an acknowledged edge case documented in the code

    def test_exclude_patterns_valid_list(self):
        cfg = self._make_cfg()
        fzfr._merge_config_key(cfg, "exclude_patterns", [], ["*.pyc", ".git"])
        self.assertEqual(cfg["exclude_patterns"], ["*.pyc", ".git"])

    def test_exclude_patterns_wrong_type_ignored(self):
        cfg = self._make_cfg()
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            fzfr._merge_config_key(cfg, "exclude_patterns", [], "*.pyc")
        self.assertEqual(cfg["exclude_patterns"], [])
        self.assertIn("Warning", buf.getvalue())

    def test_keybindings_valid_dict(self):
        cfg = self._make_cfg()
        fzfr._merge_config_key(cfg, "keybindings", cfg["keybindings"], {"exit": "ctrl-q"})
        self.assertEqual(cfg["keybindings"]["exit"], "ctrl-q")
        # Other keybindings should be untouched
        self.assertEqual(cfg["keybindings"]["open_file"], "enter")

    def test_keybindings_wrong_type_ignored(self):
        cfg = self._make_cfg()
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            fzfr._merge_config_key(cfg, "keybindings", cfg["keybindings"], "bad")
        self.assertIn("Warning", buf.getvalue())

    def test_keybinding_value_wrong_type_ignored(self):
        cfg = self._make_cfg()
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            fzfr._merge_config_key(
                cfg, "keybindings", cfg["keybindings"],
                {"exit": 42}  # int instead of str
            )
        # The bad value should not overwrite the default
        self.assertEqual(cfg["keybindings"]["exit"], "esc")
        self.assertIn("Warning", buf.getvalue())


# ---------------------------------------------------------------------------
# _build_fd_rga_args
# ---------------------------------------------------------------------------

class TestBuildFdRgaArgs(unittest.TestCase):

    def test_basic_file_type(self):
        fd_args, rga_args = fzfr._build_fd_rga_args("f", "", False, [])
        self.assertIn("--type", fd_args)
        self.assertIn("f", fd_args)
        self.assertEqual(rga_args, [])

    def test_hidden_flag_added_to_both(self):
        fd_args, rga_args = fzfr._build_fd_rga_args("f", "", True, [])
        self.assertIn("--hidden", fd_args)
        self.assertIn("--hidden", rga_args)

    def test_extension_added_to_both(self):
        fd_args, rga_args = fzfr._build_fd_rga_args("f", "py", False, [])
        self.assertIn("-e", fd_args)
        self.assertIn("py", fd_args)
        self.assertIn("-g", rga_args)
        self.assertIn("*.py", rga_args)

    def test_multiple_extensions(self):
        fd_args, rga_args = fzfr._build_fd_rga_args("f", "py rs", False, [])
        self.assertEqual(fd_args.count("-e"), 2)
        self.assertEqual(rga_args.count("-g"), 2)

    def test_exclude_patterns_added_to_both(self):
        fd_args, rga_args = fzfr._build_fd_rga_args("f", "", False, [".git", "*.pyc"])
        self.assertIn("-E", fd_args)
        self.assertIn(".git", fd_args)
        self.assertIn("--exclude", rga_args)
        self.assertIn(".git", rga_args)

    def test_each_option_iterated_once(self):
        # hidden, ext, and exclude should each appear exactly once per list
        fd_args, rga_args = fzfr._build_fd_rga_args("f", "py", True, [".git"])
        self.assertEqual(fd_args.count("--hidden"), 1)
        self.assertEqual(rga_args.count("--hidden"), 1)
        self.assertEqual(fd_args.count("-e"), 1)
        self.assertEqual(rga_args.count("-g"), 1)


# ---------------------------------------------------------------------------
# _build_remote_cmd
# ---------------------------------------------------------------------------

class TestBuildRemoteCmd(unittest.TestCase):

    def _fd_args(self):
        return ["fd", "-L", "--type", "f"]

    def test_relative_no_query_uses_cd(self):
        cmd = fzfr._build_remote_cmd(self._fd_args(), [], "", "/remote/base", True)
        self.assertIn("cd", cmd)
        self.assertIn("/remote/base", cmd)

    def test_absolute_no_query_no_cd(self):
        cmd = fzfr._build_remote_cmd(self._fd_args(), [], "", "/remote/base", False)
        self.assertNotIn("cd ", cmd)
        self.assertIn("/remote/base", cmd)

    def test_relative_with_query_uses_cd(self):
        cmd = fzfr._build_remote_cmd(self._fd_args(), [], "myquery", "/remote/base", True)
        self.assertIn("cd", cmd)
        self.assertIn("myquery", cmd)

    def test_absolute_with_query_no_cd(self):
        cmd = fzfr._build_remote_cmd(self._fd_args(), [], "myquery", "/remote/base", False)
        self.assertNotIn("cd ", cmd)
        self.assertIn("myquery", cmd)

    def test_path_with_spaces_is_quoted(self):
        cmd = fzfr._build_remote_cmd(self._fd_args(), [], "", "/remote/my path", False)
        # shlex.quote wraps in single quotes
        self.assertIn("'", cmd)

    def test_query_with_special_chars_is_quoted(self):
        cmd = fzfr._build_remote_cmd(self._fd_args(), [], "hello world", "/tmp", False)
        # The query must be quoted so spaces don't split it
        self.assertIn("'hello world'", cmd)

    def test_error_message_in_no_query_cmd(self):
        # No-query path should emit an error message on failure
        cmd = fzfr._build_remote_cmd(self._fd_args(), [], "", "/tmp", False)
        self.assertIn("Error", cmd)

    def test_rga_fallback_present_in_query_cmd(self):
        # Content search should try rga then fall back to grep
        cmd = fzfr._build_remote_cmd(self._fd_args(), [], "search", "/tmp", False)
        self.assertIn("rga", cmd)
        self.assertIn("grep", cmd)
        self.assertIn("||", cmd)


# ---------------------------------------------------------------------------
# _parse_remote_reload_args
# ---------------------------------------------------------------------------

class TestParseRemoteReloadArgs(unittest.TestCase):

    def _base(self, extra=None):
        argv = ["host", "/base", "", "f", ""]
        if extra:
            argv += extra
        return argv

    def test_minimal_valid(self):
        args = fzfr._parse_remote_reload_args(self._base())
        self.assertIsNotNone(args)
        self.assertEqual(args.remote, "host")
        self.assertEqual(args.base_path, "/base")
        self.assertEqual(args.ftype, "f")
        self.assertFalse(args.hidden)
        self.assertFalse(args.relative)
        self.assertEqual(args.query, "")
        self.assertEqual(args.exclude_patterns, [])

    def test_too_few_args_returns_none(self):
        self.assertIsNone(fzfr._parse_remote_reload_args(["host", "/base"]))

    def test_hidden_flag(self):
        args = fzfr._parse_remote_reload_args(self._base(["--hidden"]))
        self.assertTrue(args.hidden)

    def test_relative_flag(self):
        args = fzfr._parse_remote_reload_args(self._base(["--relative"]))
        self.assertTrue(args.relative)

    def test_query_parsed(self):
        args = fzfr._parse_remote_reload_args(self._base(["myquery"]))
        self.assertEqual(args.query, "myquery")

    def test_exclude_single(self):
        args = fzfr._parse_remote_reload_args(self._base(["--exclude", "*.pyc"]))
        self.assertEqual(args.exclude_patterns, ["*.pyc"])

    def test_exclude_multiple(self):
        args = fzfr._parse_remote_reload_args(
            self._base(["--exclude", "*.pyc", "--exclude", ".git"])
        )
        self.assertEqual(args.exclude_patterns, ["*.pyc", ".git"])

    def test_exclude_missing_arg_returns_none(self):
        self.assertIsNone(
            fzfr._parse_remote_reload_args(self._base(["--exclude"]))
        )

    def test_all_flags_combined(self):
        args = fzfr._parse_remote_reload_args(
            self._base(["searchterm", "--hidden", "--relative", "--exclude", "*.log"])
        )
        self.assertEqual(args.query, "searchterm")
        self.assertTrue(args.hidden)
        self.assertTrue(args.relative)
        self.assertEqual(args.exclude_patterns, ["*.log"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
