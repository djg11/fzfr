"""
test_fzfr.py — Unit tests for fzfr.

Covers the highest-risk functions: quoting, path safety, extension parsing,
config merging, remote arg building, fzf version parsing, state management,
archive classification, backend dispatch, and script self-location.

Run with:
    python3 -m pytest tests/test_fzfr.py -v
    python3 -m unittest discover -s tests -v   # no pytest required
    make test
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Module import
#
# Prefer the src/ package (clean imports, works during development).
# Fall back to the built single-file fzfr script (works after make build,
# and in CI where src/ may not be on the path).
# ---------------------------------------------------------------------------

here = Path(__file__).parent
src_path = here.parent / "src"

if src_path.exists():
    sys.path.insert(0, str(src_path))
    import fzfr
    from fzfr.utils import _parse_extensions
    from fzfr.config import _CONFIG_DEFAULTS, _merge_config_key, load_config
    from fzfr.backends import LocalBackend, RemoteBackend, backend_from_state
    from fzfr.open import _dquote
    from fzfr.remote import _build_fd_rga_args, _build_remote_cmd, _parse_remote_reload_args
    from fzfr.search import _parse_fzf_version, _find_git_root
    from fzfr._script import _is_built_script, _find_self
    from fzfr.state import _save_state, _load_state, _mutate_state
    from fzfr.workbase import _assert_not_symlink
    from fzfr.archive import classify, FileKind
    from fzfr.copy import _resolve_remote_path
else:
    import importlib.util
    from importlib.machinery import SourceFileLoader
    candidates = [here.parent / "fzfr", here.parent / "fzfr.py"]
    script = next((p for p in candidates if p.exists()), None)
    if script is None:
        raise FileNotFoundError(f"fzfr not found. Searched: {candidates}")
    loader = SourceFileLoader("fzfr", str(script))
    spec = importlib.util.spec_from_loader("fzfr", loader)
    fzfr = importlib.util.module_from_spec(spec)
    loader.exec_module(fzfr)
    _parse_extensions = fzfr._parse_extensions
    _CONFIG_DEFAULTS = fzfr._CONFIG_DEFAULTS
    _merge_config_key = fzfr._merge_config_key
    load_config = fzfr.load_config
    LocalBackend = fzfr.LocalBackend
    RemoteBackend = fzfr.RemoteBackend
    backend_from_state = fzfr.backend_from_state
    _dquote = fzfr._dquote
    _build_fd_rga_args = fzfr._build_fd_rga_args
    _build_remote_cmd = fzfr._build_remote_cmd
    _parse_remote_reload_args = fzfr._parse_remote_reload_args
    _parse_fzf_version = fzfr._parse_fzf_version
    _find_git_root = fzfr._find_git_root
    _is_built_script = fzfr._is_built_script
    _find_self = fzfr._find_self
    _save_state = fzfr._save_state
    _load_state = fzfr._load_state
    _mutate_state = fzfr._mutate_state
    _assert_not_symlink = fzfr._assert_not_symlink
    classify = fzfr.classify
    FileKind = fzfr.FileKind
    _resolve_remote_path = fzfr._resolve_remote_path


# ---------------------------------------------------------------------------
# _parse_extensions
# ---------------------------------------------------------------------------

class TestParseExtensions(unittest.TestCase):

    def test_basic(self):
        self.assertEqual(_parse_extensions("py txt"), ["py", "txt"])

    def test_dot_prefix_stripped(self):
        self.assertEqual(_parse_extensions(".py .txt"), ["py", "txt"])

    def test_empty_string(self):
        self.assertEqual(_parse_extensions(""), [])

    def test_duplicates_preserved(self):
        result = _parse_extensions("py py")
        self.assertEqual(result, ["py", "py"])

    def test_single_extension(self):
        self.assertEqual(_parse_extensions("rs"), ["rs"])

    def test_unsafe_chars_rejected(self):
        self.assertEqual(_parse_extensions("py;evil"), [])
        self.assertEqual(_parse_extensions("py$(cmd)"), [])
        self.assertEqual(_parse_extensions("py`cmd`"), [])

    def test_numeric_extensions_allowed(self):
        self.assertEqual(_parse_extensions("mp3 mp4 h264"), ["mp3", "mp4", "h264"])

    def test_mixed_valid_and_invalid(self):
        result = _parse_extensions("py ;evil txt")
        self.assertIn("py", result)
        self.assertIn("txt", result)
        self.assertNotIn(";evil", result)


# ---------------------------------------------------------------------------
# _parse_fzf_version
# ---------------------------------------------------------------------------

class TestParseFzfVersion(unittest.TestCase):

    def test_standard_version(self):
        self.assertEqual(_parse_fzf_version("0.44.1"), (0, 44))

    def test_major_minor_only(self):
        self.assertEqual(_parse_fzf_version("1.2"), (1, 2))

    def test_garbage_returns_zero(self):
        self.assertEqual(_parse_fzf_version("garbage"), (0, 0))

    def test_empty_returns_zero(self):
        self.assertEqual(_parse_fzf_version(""), (0, 0))

    def test_version_with_non_numeric_prefix_returns_zero(self):
        # The parser expects a bare version string, not "fzf 0.38.0"
        self.assertEqual(_parse_fzf_version("fzf 0.38.0"), (0, 0))

    def test_high_version_numbers(self):
        self.assertEqual(_parse_fzf_version("2.100.5"), (2, 100))


# ---------------------------------------------------------------------------
# _dquote
# ---------------------------------------------------------------------------

class TestDquote(unittest.TestCase):

    def test_simple_path(self):
        self.assertEqual(_dquote("/home/user/file.txt"), '"/home/user/file.txt"')

    def test_path_with_spaces(self):
        self.assertEqual(_dquote("/path/with spaces/file"), '"/path/with spaces/file"')

    def test_dollar_sign_escaped(self):
        result = _dquote("$HOME/file")
        self.assertIn("\\$", result)

    def test_backtick_escaped(self):
        result = _dquote("`cmd`")
        self.assertIn("\\`", result)

    def test_backslash_escaped(self):
        result = _dquote("path\\file")
        self.assertIn("\\\\", result)

    def test_double_quote_escaped(self):
        result = _dquote('say "hello"')
        self.assertNotEqual(result.count('"'), 2)  # inner quotes must be escaped

    def test_exclamation_escaped(self):
        result = _dquote("file!name")
        self.assertIn("\\!", result)

    def test_empty_string(self):
        self.assertEqual(_dquote(""), '""')

    def test_result_is_double_quoted(self):
        result = _dquote("anything")
        self.assertTrue(result.startswith('"'))
        self.assertTrue(result.endswith('"'))


# ---------------------------------------------------------------------------
# LocalBackend.is_safe_subpath
# ---------------------------------------------------------------------------

class TestLocalBackendIsSafeSubpath(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.be = LocalBackend(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_base_path_itself_is_safe(self):
        self.assertTrue(self.be.is_safe_subpath(self.tmp))

    def test_subdir_is_safe(self):
        sub = os.path.join(self.tmp, "subdir")
        os.makedirs(sub)
        self.assertTrue(self.be.is_safe_subpath(sub))

    def test_etc_is_not_safe(self):
        self.assertFalse(self.be.is_safe_subpath("/etc/passwd"))

    def test_traversal_is_not_safe(self):
        self.assertFalse(self.be.is_safe_subpath(self.tmp + "/../etc"))

    def test_sibling_dir_is_not_safe(self):
        sibling = tempfile.mkdtemp()
        try:
            self.assertFalse(self.be.is_safe_subpath(sibling))
        finally:
            shutil.rmtree(sibling, ignore_errors=True)

    def test_nonexistent_subpath_is_safe_if_would_be_inside(self):
        # A path that doesn't exist yet but resolves inside the base
        inside = os.path.join(self.tmp, "new", "file.txt")
        self.assertTrue(self.be.is_safe_subpath(inside))


# ---------------------------------------------------------------------------
# _merge_config_key
# ---------------------------------------------------------------------------

class TestMergeConfigKey(unittest.TestCase):

    def test_user_value_overrides_default(self):
        cfg = dict(_CONFIG_DEFAULTS)
        _merge_config_key(cfg, "editor", "", "nvim")
        self.assertEqual(cfg["editor"], "nvim")

    def test_missing_user_value_keeps_default(self):
        cfg = dict(_CONFIG_DEFAULTS)
        original = cfg["editor"]
        _merge_config_key(cfg, "editor", original, None)
        self.assertEqual(cfg["editor"], original)

    def test_wrong_type_uses_default(self):
        cfg = dict(_CONFIG_DEFAULTS)
        _merge_config_key(cfg, "show_hidden", False, "yes")
        self.assertEqual(cfg["show_hidden"], False)

    def test_bool_true_accepted(self):
        cfg = dict(_CONFIG_DEFAULTS)
        _merge_config_key(cfg, "show_hidden", False, True)
        self.assertTrue(cfg["show_hidden"])

    def test_int_accepted(self):
        cfg = dict(_CONFIG_DEFAULTS)
        _merge_config_key(cfg, "ssh_control_persist", 60, 120)
        self.assertEqual(cfg["ssh_control_persist"], 120)

    def test_list_accepted(self):
        cfg = dict(_CONFIG_DEFAULTS)
        _merge_config_key(cfg, "exclude_patterns", [], [".git", "*.pyc"])
        self.assertEqual(cfg["exclude_patterns"], [".git", "*.pyc"])

    def test_dict_merged(self):
        cfg = dict(_CONFIG_DEFAULTS)
        default_kb = {"toggle_mode": "ctrl-t", "exit": "esc"}
        user_kb = {"toggle_mode": "ctrl-g"}
        _merge_config_key(cfg, "keybindings", default_kb, user_kb)
        self.assertEqual(cfg["keybindings"]["toggle_mode"], "ctrl-g")
        self.assertEqual(cfg["keybindings"]["exit"], "esc")

    def test_empty_string_user_value_accepted(self):
        # Empty string is a valid user override (clears the editor override)
        cfg = dict(_CONFIG_DEFAULTS)
        _merge_config_key(cfg, "editor", "vim", "")
        self.assertEqual(cfg["editor"], "")


# ---------------------------------------------------------------------------
# _build_fd_rga_args
# ---------------------------------------------------------------------------

class TestBuildFdRgaArgs(unittest.TestCase):

    def test_basic_file_type(self):
        fd_args, rga_args = _build_fd_rga_args("f", "", False, [])
        self.assertIn("--type", fd_args)
        self.assertIn("f", fd_args)

    def test_hidden_flag_added_to_both(self):
        fd_args, rga_args = _build_fd_rga_args("f", "", True, [])
        self.assertIn("--hidden", fd_args)
        self.assertIn("--hidden", rga_args)

    def test_extension_added_to_both(self):
        fd_args, rga_args = _build_fd_rga_args("f", "py", False, [])
        self.assertTrue(any("py" in a for a in fd_args))
        self.assertTrue(any("py" in a for a in rga_args))

    def test_exclude_patterns_added_to_both(self):
        fd_args, rga_args = _build_fd_rga_args("f", "", False, [".git"])
        self.assertIn(".git", " ".join(fd_args))
        self.assertIn(".git", " ".join(rga_args))

    def test_each_option_iterated_once(self):
        fd_args, rga_args = _build_fd_rga_args("f", "py", True, [".git"])
        self.assertEqual(fd_args.count("--hidden"), 1)
        self.assertEqual(rga_args.count("--hidden"), 1)

    def test_directory_type(self):
        fd_args, rga_args = _build_fd_rga_args("d", "", False, [])
        self.assertIn("d", fd_args)


# ---------------------------------------------------------------------------
# _build_remote_cmd
# ---------------------------------------------------------------------------

class TestBuildRemoteCmd(unittest.TestCase):

    def _base_args(self, **kwargs):
        defaults = dict(
            fd_args=["fd", "-L", "--type", "f"],
            rga_glob_args=[],
            query="",
            base_path="/home/user",
            relative=False,
        )
        defaults.update(kwargs)
        return defaults

    def test_returns_string(self):
        result = _build_remote_cmd(**self._base_args())
        self.assertIsInstance(result, str)

    def test_contains_base_path(self):
        result = _build_remote_cmd(**self._base_args(base_path="/var/log"))
        self.assertIn("/var/log", result)

    def test_query_included_when_present(self):
        result = _build_remote_cmd(**self._base_args(query="error"))
        self.assertIn("error", result)

    def test_no_query_uses_fd_only(self):
        result = _build_remote_cmd(**self._base_args(query=""))
        self.assertIn("fd", result)

    def test_relative_flag_affects_output(self):
        abs_result = _build_remote_cmd(**self._base_args(relative=False))
        rel_result = _build_remote_cmd(**self._base_args(relative=True))
        self.assertNotEqual(abs_result, rel_result)


# ---------------------------------------------------------------------------
# _parse_remote_reload_args
# ---------------------------------------------------------------------------

class TestParseRemoteReloadArgs(unittest.TestCase):

    def _base(self, extra=None):
        base = ["user@host", "/base/path", "/tmp/ssh_ctl", "f", ""]
        return base + (extra or [])

    def test_basic_parse(self):
        args = _parse_remote_reload_args(self._base())
        self.assertEqual(args.remote, "user@host")
        self.assertEqual(args.base_path, "/base/path")
        self.assertEqual(args.ssh_control, "/tmp/ssh_ctl")

    def test_too_few_args_returns_none(self):
        self.assertIsNone(_parse_remote_reload_args(["host"]))

    def test_query_parsed(self):
        args = _parse_remote_reload_args(self._base(["myquery"]))
        self.assertEqual(args.query, "myquery")

    def test_hidden_flag(self):
        args = _parse_remote_reload_args(self._base(["--hidden"]))
        self.assertTrue(args.hidden)

    def test_relative_flag(self):
        args = _parse_remote_reload_args(self._base(["--relative"]))
        self.assertTrue(args.relative)

    def test_exclude_single(self):
        args = _parse_remote_reload_args(self._base(["--exclude", "*.pyc"]))
        self.assertEqual(args.exclude_patterns, ["*.pyc"])

    def test_exclude_multiple(self):
        args = _parse_remote_reload_args(
            self._base(["--exclude", "*.pyc", "--exclude", ".git"])
        )
        self.assertEqual(args.exclude_patterns, ["*.pyc", ".git"])

    def test_exclude_missing_arg_returns_none(self):
        self.assertIsNone(_parse_remote_reload_args(self._base(["--exclude"])))

    def test_all_flags_combined(self):
        args = _parse_remote_reload_args(
            self._base(["searchterm", "--hidden", "--relative", "--exclude", "*.log"])
        )
        self.assertEqual(args.query, "searchterm")
        self.assertTrue(args.hidden)
        self.assertTrue(args.relative)
        self.assertEqual(args.exclude_patterns, ["*.log"])


# ---------------------------------------------------------------------------
# _is_built_script / _find_self
# ---------------------------------------------------------------------------

class TestIsBuiltScript(unittest.TestCase):

    def test_file_with_shebang_is_built(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"#!/usr/bin/env python3\nprint('hello')\n")
            path = Path(f.name)
        try:
            self.assertTrue(_is_built_script(path))
        finally:
            path.unlink()

    def test_file_without_shebang_is_not_built(self):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"VERSION = 'v0.9.1'\n")
            path = Path(f.name)
        try:
            self.assertFalse(_is_built_script(path))
        finally:
            path.unlink()

    def test_empty_file_is_not_built(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = Path(f.name)
        try:
            self.assertFalse(_is_built_script(path))
        finally:
            path.unlink()

    def test_nonexistent_path_returns_false(self):
        self.assertFalse(_is_built_script(Path("/nonexistent/path/fzfr")))

    def test_partial_shebang_is_not_built(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"#!/usr/bin/env pyth")  # truncated before "on3"
            path = Path(f.name)
        try:
            self.assertFalse(_is_built_script(path))
        finally:
            path.unlink()


class TestFindSelf(unittest.TestCase):

    def test_returns_string_or_none(self):
        result = _find_self()
        self.assertTrue(result is None or isinstance(result, str))

    def test_returned_path_exists_if_not_none(self):
        result = _find_self()
        if result is not None:
            self.assertTrue(Path(result).exists(), f"Path does not exist: {result}")

    def test_returned_file_has_shebang(self):
        result = _find_self()
        if result is not None:
            self.assertTrue(
                _is_built_script(Path(result)),
                f"Found file has no shebang: {result}",
            )


# ---------------------------------------------------------------------------
# backend_from_state
# ---------------------------------------------------------------------------

class TestBackendFromState(unittest.TestCase):

    def _state(self, remote="", base_path="/tmp", ssh_control="", exclude=None):
        return {
            "remote": remote,
            "base_path": base_path,
            "ssh_control": ssh_control,
            "exclude_patterns": exclude or [],
        }

    def test_empty_remote_returns_local(self):
        self.assertIsInstance(backend_from_state(self._state()), LocalBackend)

    def test_local_string_treated_as_ssh_host(self):
        # "local" is handled by cmd_search (target == "local" branch) before
        # backend_from_state is ever called. State saved for local sessions
        # always has remote="" not remote="local". This test documents that
        # backend_from_state treats any non-empty string as an SSH host.
        self.assertIsInstance(backend_from_state(self._state(remote="local")), RemoteBackend)

    def test_ssh_host_returns_remote(self):
        self.assertIsInstance(backend_from_state(self._state(remote="user@host")), RemoteBackend)

    def test_hostname_only_returns_remote(self):
        self.assertIsInstance(backend_from_state(self._state(remote="myserver")), RemoteBackend)

    def test_local_base_path_set(self):
        be = backend_from_state(self._state(base_path="/home/user"))
        self.assertEqual(be.base_path, "/home/user")

    def test_remote_host_and_path_set(self):
        be = backend_from_state(self._state(remote="myserver", base_path="/data"))
        self.assertEqual(be.remote, "myserver")
        self.assertEqual(be.base_path, "/data")

    def test_exclude_patterns_passed_through(self):
        be = backend_from_state(self._state(exclude=[".git", "*.pyc"]))
        self.assertEqual(be.exclude_patterns, [".git", "*.pyc"])

    def test_empty_exclude_gives_empty_list(self):
        be = backend_from_state(self._state())
        self.assertEqual(be.exclude_patterns, [])


# ---------------------------------------------------------------------------
# _save_state / _load_state / _mutate_state
# ---------------------------------------------------------------------------

class TestState(unittest.TestCase):

    def setUp(self):
        from fzfr.workbase import WORK_BASE
        WORK_BASE.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.mkdtemp(dir=WORK_BASE)
        self.state_path = Path(self.tmp) / "state.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_load_roundtrip(self):
        state = {"mode": "content", "remote": "", "show_hidden": False}
        _save_state(self.state_path, state)
        loaded = _load_state(self.state_path)
        self.assertEqual(loaded, state)

    def test_load_missing_file_returns_empty_dict(self):
        self.assertEqual(_load_state(Path(self.tmp) / "missing.json"), {})

    def test_save_leaves_no_tmp_file(self):
        _save_state(self.state_path, {"k": "v"})
        self.assertTrue(self.state_path.exists())
        self.assertFalse(self.state_path.with_suffix(".tmp").exists())

    def test_mutate_applies_function(self):
        _save_state(self.state_path, {"mode": "name"})
        rc = _mutate_state(self.state_path, lambda s: s.update({"mode": "content"}))
        self.assertEqual(rc, 0)
        self.assertEqual(_load_state(self.state_path)["mode"], "content")

    def test_mutate_missing_file_returns_1(self):
        rc = _mutate_state(Path(self.tmp) / "missing.json", lambda s: None)
        self.assertEqual(rc, 1)

    def test_multiple_keys_preserved_on_mutate(self):
        _save_state(self.state_path, {"mode": "name", "hidden": False, "ext": "py"})
        _mutate_state(self.state_path, lambda s: s.update({"hidden": True}))
        loaded = _load_state(self.state_path)
        self.assertEqual(loaded["mode"], "name")
        self.assertEqual(loaded["ext"], "py")
        self.assertTrue(loaded["hidden"])

    def test_load_outside_workbase_returns_empty(self):
        # SECURITY: _load_state rejects paths outside WORK_BASE — state paths
        # arrive as argv elements from fzf callbacks and must not be attacker-
        # controlled paths outside the session directory.
        outside = Path("/tmp/fzfr_test_outside_workbase_xyz.json")
        result = _load_state(outside)
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# _assert_not_symlink
# ---------------------------------------------------------------------------

class TestAssertNotSymlink(unittest.TestCase):

    def test_regular_directory_passes(self):
        with tempfile.TemporaryDirectory() as d:
            _assert_not_symlink(Path(d))  # must not raise

    def test_regular_file_passes(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = Path(f.name)
        try:
            _assert_not_symlink(path)  # must not raise
        finally:
            path.unlink()

    def test_nonexistent_path_passes(self):
        _assert_not_symlink(Path("/nonexistent/xyz_fzfr_test"))  # must not raise

    def test_symlink_exits(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "target"
            target.mkdir()
            link = Path(d) / "link"
            link.symlink_to(target)
            with self.assertRaises(SystemExit):
                _assert_not_symlink(link)


# ---------------------------------------------------------------------------
# classify / FileKind
# ---------------------------------------------------------------------------

class TestClassify(unittest.TestCase):

    def test_pdf(self):
        self.assertEqual(classify("report.pdf"), FileKind.PDF)

    def test_text_extensions(self):
        for ext in ["py", "txt", "md", "rs", "go", "js", "json", "yaml", "c", "h"]:
            with self.subTest(ext=ext):
                self.assertEqual(classify(f"file.{ext}"), FileKind.TEXT)

    def test_archive_simple(self):
        for ext in ["zip", "tar", "7z", "rar"]:
            with self.subTest(ext=ext):
                self.assertEqual(classify(f"archive.{ext}"), FileKind.ARCHIVE)

    def test_archive_compound(self):
        for name in ["archive.tar.gz", "archive.tar.bz2", "archive.tar.xz"]:
            with self.subTest(name=name):
                self.assertEqual(classify(name), FileKind.ARCHIVE)

    def test_text_mime_overrides_unknown_extension(self):
        self.assertEqual(classify("oddfile.xyz", mime="text/plain"), FileKind.TEXT)

    def test_empty_inode_mime_is_text(self):
        self.assertEqual(classify("empty", mime="inode/x-empty"), FileKind.TEXT)

    def test_binary_mime(self):
        self.assertEqual(classify("blob.bin", mime="application/octet-stream"), FileKind.BINARY)

    def test_unknown_extension_no_mime_is_text(self):
        # Unknown extensions default to TEXT (safe fallback for bat previewer)
        self.assertEqual(classify("unknown.xyz"), FileKind.TEXT)

    def test_case_insensitive(self):
        self.assertEqual(classify("ARCHIVE.ZIP"), FileKind.ARCHIVE)
        self.assertEqual(classify("REPORT.PDF"), FileKind.PDF)

    def test_pdf_mime_overrides_extension(self):
        self.assertEqual(classify("file.txt", mime="application/pdf"), FileKind.PDF)


# ---------------------------------------------------------------------------
# _find_git_root
# ---------------------------------------------------------------------------

class TestFindGitRoot(unittest.TestCase):

    def test_returns_string_or_none(self):
        result = _find_git_root()
        self.assertTrue(result is None or isinstance(result, str))

    def test_returned_path_contains_dot_git(self):
        result = _find_git_root()
        if result is not None:
            self.assertTrue(
                (Path(result) / ".git").exists(),
                f"Expected .git in {result}",
            )

    def test_outside_repo_returns_none(self):
        import subprocess
        old_cwd = os.getcwd()
        try:
            os.chdir("/tmp")
            check = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, cwd="/tmp",
            )
            if check.returncode == 0:
                self.skipTest("/tmp happens to be inside a git repo")
            self.assertIsNone(_find_git_root())
        finally:
            os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

class TestLoadConfig(unittest.TestCase):

    def test_returns_dict(self):
        self.assertIsInstance(load_config(), dict)

    def test_contains_all_default_keys(self):
        cfg = load_config()
        for key in _CONFIG_DEFAULTS:
            with self.subTest(key=key):
                self.assertIn(key, cfg)

    def test_keybindings_is_dict(self):
        cfg = load_config()
        self.assertIsInstance(cfg["keybindings"], dict)

    def test_default_mode_is_valid(self):
        cfg = load_config()
        self.assertIn(cfg["default_mode"], ("name", "content"))

    def test_show_hidden_is_bool(self):
        cfg = load_config()
        self.assertIsInstance(cfg["show_hidden"], bool)

    def test_exclude_patterns_is_list(self):
        cfg = load_config()
        self.assertIsInstance(cfg["exclude_patterns"], list)

    def test_max_stream_mb_is_positive_int(self):
        cfg = load_config()
        self.assertIsInstance(cfg["max_stream_mb"], int)
        self.assertGreaterEqual(cfg["max_stream_mb"], 0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
