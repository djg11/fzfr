"""
test_remotely.py -- Unit tests for remotely.

Covers the highest-risk functions: quoting, path safety, extension parsing,
config merging, remote arg building, fzf version parsing, state management,
archive classification, backend dispatch, and script self-location.

Run with:
    python3 -m pytest tests/test_remotely.py -v
    python3 -m unittest discover -s tests -v   # no pytest required
    make test
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Module import
#
# Prefer the src/ package (clean imports, works during development).
# Fall back to the built single-file remotely script (works after make build).
# ---------------------------------------------------------------------------

here = Path(__file__).parent
src_path = here.parent / "src"

if src_path.exists():
    sys.path.insert(0, str(src_path))
    import remotely
    from remotely._script import _find_self, _is_built_script
    from remotely.archive import FileKind, classify
    from remotely.backends import (
        LocalBackend,
        RemoteBackend,
        _find_git_root,
        backend_from_state,
    )
    from remotely.config import _CONFIG_DEFAULTS, _merge_config_key, load_config
    from remotely.remote import (
        _build_fd_rga_args,
        _build_remote_cmd,
        _parse_remote_reload_args,
    )
    from remotely.state import _load_state, _mutate_state, _save_state
    from remotely.utils import (
        _parse_extensions,
        _parse_fzf_version,
        _resolve_remote_path,
        _validate_exclude_pattern,
    )
    from remotely.workbase import _assert_not_symlink
else:
    import importlib.util
    from importlib.machinery import SourceFileLoader

    candidates = [here.parent / "remotely", here.parent / "remotely.py"]
    script = next((p for p in candidates if p.exists()), None)
    if script is None:
        raise FileNotFoundError(f"remotely not found. Searched: {candidates}")
    loader = SourceFileLoader("remotely", str(script))
    spec = importlib.util.spec_from_loader("remotely", loader)
    remotely = importlib.util.module_from_spec(spec)
    loader.exec_module(remotely)
    _parse_extensions = remotely._parse_extensions
    _CONFIG_DEFAULTS = remotely._CONFIG_DEFAULTS
    _merge_config_key = remotely._merge_config_key
    load_config = remotely.load_config
    LocalBackend = remotely.LocalBackend
    RemoteBackend = remotely.RemoteBackend
    backend_from_state = remotely.backend_from_state
    _build_fd_rga_args = remotely._build_fd_rga_args
    _build_remote_cmd = remotely._build_remote_cmd
    _parse_remote_reload_args = remotely._parse_remote_reload_args
    _parse_fzf_version = remotely._parse_fzf_version
    _find_git_root = remotely._find_git_root
    _is_built_script = remotely._is_built_script
    _find_self = remotely._find_self
    _save_state = remotely._save_state
    _load_state = remotely._load_state
    _mutate_state = remotely._mutate_state
    _assert_not_symlink = remotely._assert_not_symlink
    classify = remotely.classify
    FileKind = remotely.FileKind
    _resolve_remote_path = remotely._resolve_remote_path
    _validate_exclude_pattern = remotely._validate_exclude_pattern


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
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(_parse_extensions("py;evil"), [])
            self.assertEqual(_parse_extensions("py$(cmd)"), [])
            self.assertEqual(_parse_extensions("py`cmd`"), [])

    def test_numeric_extensions_allowed(self):
        self.assertEqual(_parse_extensions("mp3 mp4 h264"), ["mp3", "mp4", "h264"])

    def test_mixed_valid_and_invalid(self):
        with contextlib.redirect_stderr(io.StringIO()):
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
        self.assertEqual(_parse_fzf_version("fzf 0.38.0"), (0, 0))

    def test_high_version_numbers(self):
        self.assertEqual(_parse_fzf_version("2.100.5"), (2, 100))


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
        with contextlib.redirect_stderr(io.StringIO()):
            _merge_config_key(cfg, "editor", original, None)
        self.assertEqual(cfg["editor"], original)

    def test_wrong_type_uses_default(self):
        cfg = dict(_CONFIG_DEFAULTS)
        with contextlib.redirect_stderr(io.StringIO()):
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

    def test_empty_string_user_value_accepted(self):
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
        with contextlib.redirect_stderr(io.StringIO()):
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
        with contextlib.redirect_stderr(io.StringIO()):
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
        self.assertFalse(_is_built_script(Path("/nonexistent/path/remotely")))

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
        # "local" is handled before backend_from_state is called;
        # any non-empty string is treated as an SSH host here.
        self.assertIsInstance(
            backend_from_state(self._state(remote="local")), RemoteBackend
        )

    def test_ssh_host_returns_remote(self):
        self.assertIsInstance(
            backend_from_state(self._state(remote="user@host")), RemoteBackend
        )

    def test_hostname_only_returns_remote(self):
        self.assertIsInstance(
            backend_from_state(self._state(remote="myserver")), RemoteBackend
        )

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
        from remotely.workbase import WORK_BASE

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
        # SECURITY: _load_state rejects paths outside WORK_BASE.
        outside = Path("/tmp/remotely_test_outside_workbase_xyz.json")
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
        _assert_not_symlink(Path("/nonexistent/xyz_remotely_test"))  # must not raise

    def test_symlink_exits(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "target"
            target.mkdir()
            link = Path(d) / "link"
            link.symlink_to(target)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(
                io.StringIO()
            ):
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
        self.assertEqual(
            classify("blob.bin", mime="application/octet-stream"), FileKind.BINARY
        )

    def test_unknown_extension_no_mime_is_text(self):
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
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd="/tmp",
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
# Security
# ---------------------------------------------------------------------------


class TestSecurity(unittest.TestCase):
    def test_validate_exclude_pattern_safe(self):
        safe_patterns = ["*.py", "dist/", "node_modules", "foo?bar", "baz[0-9]"]
        for p in safe_patterns:
            with self.subTest(pattern=p):
                self.assertTrue(_validate_exclude_pattern(p))

    def test_validate_exclude_pattern_unsafe(self):
        unsafe_patterns = [
            "*.py;id",
            "foo|bar",
            "a&&b",
            "a||b",
            "$(id)",
            "`id`",
            ">out",
            "<in",
            "foo\nbar",
            "(subshell)",
            "bg&",
            r"escape\\",
        ]
        for p in unsafe_patterns:
            with self.subTest(pattern=p):
                self.assertFalse(_validate_exclude_pattern(p))

    @patch("subprocess.run")
    def test_resolve_remote_path_tilde_expansion_safe(self, mock_run):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b"/home/user/foo\n"
        mock_run.return_value = mock_result

        result = _resolve_remote_path("user@host", "~/foo", "")

        self.assertEqual(result, "/home/user/foo")

        args, kwargs = mock_run.call_args
        cmd = args[0]
        self.assertIn("python3", " ".join(cmd))
        self.assertIn("os.path.expanduser", " ".join(cmd))
        self.assertEqual(kwargs["input"], b"~/foo")

    @patch("subprocess.run")
    def test_resolve_remote_path_dot_safe(self, mock_run):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "/current/dir\n"
        mock_run.return_value = mock_result

        result = _resolve_remote_path("host", ".", "")
        self.assertEqual(result, "/current/dir")

        cmd = mock_run.call_args[0][0]
        self.assertIn("pwd", cmd)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
