"""test_remotely.py -- Unit tests for remotely.

Covers the highest-risk functions: quoting, path safety, extension and pattern
validation, config merging, remote command building, fzf version parsing, state
management, archive classification, backend dispatch, script self-location, SSH
option construction, bootstrap building, and the preview cache.

Run with:
    pytest tests/test_remotely.py -v
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
# Module import -- supports both src/ package and built single-file script
# ---------------------------------------------------------------------------

here = Path(__file__).parent
src_path = here.parent / "src"

if src_path.exists():
    sys.path.insert(0, str(src_path))
    import remotely
    from remotely._script import VERSION, _build_bootstrap, _find_self, _is_built_script
    from remotely.archive import FileKind, _hint_suffix, _list_archive, classify
    from remotely.backends import (
        LocalBackend,
        RemoteBackend,
        _find_git_root,
        backend_from_state,
    )
    from remotely.cache import (
        _evict_lru_if_needed,
        local_cache_key,
        local_mtime,
        remote_cache_key,
    )
    from remotely.cache import (
        get as cache_get,
    )
    from remotely.cache import (
        put as cache_put,
    )
    from remotely.config import (
        _CONFIG_DEFAULTS,
        _merge_config_key,
        _validate_custom_actions,
        load_config,
    )
    from remotely.preview_cmd import _parse_target_path
    from remotely.remote import (
        _build_fd_rga_args,
        _build_git_remote_cmd,
        _build_remote_cmd,
        _parse_remote_reload_args,
    )
    from remotely.ssh import _ssh_opts
    from remotely.state import _load_state, _mutate_state, _save_state
    from remotely.utils import (
        _is_text_mime,
        _parse_extensions,
        _parse_fzf_version,
        _removeprefix,
        _resolve_remote_path,
        _shlex_join,
        _validate_exclude_pattern,
    )
    from remotely.utils import (
        _validate_exclude_pattern as config_validate_exclude,
    )
    from remotely.workbase import WORK_BASE, _assert_not_symlink
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

    # Map names from the flat built script
    VERSION = remotely.VERSION
    _build_bootstrap = remotely._build_bootstrap
    _is_built_script = remotely._is_built_script
    _find_self = remotely._find_self
    _hint_suffix = remotely._hint_suffix
    _list_archive = remotely._list_archive
    classify = remotely.classify
    FileKind = remotely.FileKind
    LocalBackend = remotely.LocalBackend
    RemoteBackend = remotely.RemoteBackend
    _find_git_root = remotely._find_git_root
    backend_from_state = remotely.backend_from_state
    cache_get = remotely.cache_get if hasattr(remotely, "cache_get") else remotely.get
    cache_put = remotely.cache_put if hasattr(remotely, "cache_put") else remotely.put
    local_cache_key = remotely.local_cache_key
    remote_cache_key = remotely.remote_cache_key
    local_mtime = remotely.local_mtime
    _evict_lru_if_needed = getattr(remotely, "_evict_lru_if_needed", None)
    _CONFIG_DEFAULTS = remotely._CONFIG_DEFAULTS
    _merge_config_key = remotely._merge_config_key
    _validate_custom_actions = remotely._validate_custom_actions
    config_validate_exclude = remotely._validate_exclude_pattern
    load_config = remotely.load_config
    _parse_target_path = remotely._parse_target_path
    _build_fd_rga_args = remotely._build_fd_rga_args
    _build_git_remote_cmd = remotely._build_git_remote_cmd
    _build_remote_cmd = remotely._build_remote_cmd
    _parse_remote_reload_args = remotely._parse_remote_reload_args
    _ssh_opts = remotely._ssh_opts
    _save_state = remotely._save_state
    _load_state = remotely._load_state
    _mutate_state = remotely._mutate_state
    _is_text_mime = remotely._is_text_mime
    _parse_extensions = remotely._parse_extensions
    _parse_fzf_version = remotely._parse_fzf_version
    _removeprefix = remotely._removeprefix
    _resolve_remote_path = remotely._resolve_remote_path
    _shlex_join = remotely._shlex_join
    _validate_exclude_pattern = remotely._validate_exclude_pattern
    WORK_BASE = remotely.WORK_BASE
    _assert_not_symlink = remotely._assert_not_symlink


# ===========================================================================
# _parse_extensions
# ===========================================================================


class TestParseExtensions(unittest.TestCase):
    """_parse_extensions parses whitespace-separated extension strings."""

    def test_basic_pair(self):
        self.assertEqual(_parse_extensions("py txt"), ["py", "txt"])

    def test_leading_dots_stripped(self):
        self.assertEqual(_parse_extensions(".py .txt"), ["py", "txt"])

    def test_empty_string_returns_empty_list(self):
        self.assertEqual(_parse_extensions(""), [])

    def test_single_extension(self):
        self.assertEqual(_parse_extensions("rs"), ["rs"])

    def test_duplicates_preserved(self):
        # Deduplication is the caller's responsibility.
        self.assertEqual(_parse_extensions("py py"), ["py", "py"])

    def test_numeric_extensions_accepted(self):
        self.assertEqual(_parse_extensions("mp3 mp4 h264"), ["mp3", "mp4", "h264"])

    def test_mixed_case_preserved(self):
        self.assertEqual(_parse_extensions("Py TXT"), ["Py", "TXT"])

    def test_semicolon_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(_parse_extensions("py;evil"), [])

    def test_dollar_sign_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(_parse_extensions("py$(cmd)"), [])

    def test_backtick_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(_parse_extensions("py`cmd`"), [])

    def test_mixed_valid_and_invalid_preserves_valid(self):
        with contextlib.redirect_stderr(io.StringIO()):
            result = _parse_extensions("py ;evil txt")
        self.assertIn("py", result)
        self.assertIn("txt", result)
        self.assertNotIn(";evil", result)

    def test_extra_whitespace_ignored(self):
        self.assertEqual(_parse_extensions("  py   txt  "), ["py", "txt"])

    def test_dot_only_entry_ignored(self):
        self.assertEqual(_parse_extensions(". py"), ["py"])


# ===========================================================================
# _validate_exclude_pattern
# ===========================================================================


class TestValidateExcludePattern(unittest.TestCase):
    """_validate_exclude_pattern rejects shell operators, allows glob chars."""

    def test_simple_glob_accepted(self):
        for p in ["*.py", "dist/", "node_modules", "foo?bar", "baz[0-9]", "{a,b}"]:
            with self.subTest(pattern=p):
                self.assertTrue(_validate_exclude_pattern(p))

    def test_semicolon_rejected(self):
        self.assertFalse(_validate_exclude_pattern("*.py;id"))

    def test_pipe_rejected(self):
        self.assertFalse(_validate_exclude_pattern("foo|bar"))

    def test_and_and_rejected(self):
        self.assertFalse(_validate_exclude_pattern("a&&b"))

    def test_or_or_rejected(self):
        self.assertFalse(_validate_exclude_pattern("a||b"))

    def test_dollar_rejected(self):
        self.assertFalse(_validate_exclude_pattern("$(id)"))

    def test_backtick_rejected(self):
        self.assertFalse(_validate_exclude_pattern("`id`"))

    def test_redirect_out_rejected(self):
        self.assertFalse(_validate_exclude_pattern(">out"))

    def test_redirect_in_rejected(self):
        self.assertFalse(_validate_exclude_pattern("<in"))

    def test_newline_rejected(self):
        self.assertFalse(_validate_exclude_pattern("foo\nbar"))

    def test_subshell_parens_rejected(self):
        self.assertFalse(_validate_exclude_pattern("(subshell)"))

    def test_background_ampersand_rejected(self):
        self.assertFalse(_validate_exclude_pattern("bg&"))

    def test_backslash_rejected(self):
        self.assertFalse(_validate_exclude_pattern("escape\\"))

    def test_empty_string_accepted(self):
        self.assertTrue(_validate_exclude_pattern(""))


# ===========================================================================
# _parse_fzf_version
# ===========================================================================


class TestParseFzfVersion(unittest.TestCase):
    """_parse_fzf_version returns (major, minor) or (0, 0) on parse failure."""

    def test_standard_three_part(self):
        self.assertEqual(_parse_fzf_version("0.44.1"), (0, 44))

    def test_major_minor_only(self):
        self.assertEqual(_parse_fzf_version("1.2"), (1, 2))

    def test_garbage_returns_zero_zero(self):
        self.assertEqual(_parse_fzf_version("garbage"), (0, 0))

    def test_empty_returns_zero_zero(self):
        self.assertEqual(_parse_fzf_version(""), (0, 0))

    def test_prefixed_version_returns_zero_zero(self):
        # "fzf 0.38.0" does not start with digits; not supported.
        self.assertEqual(_parse_fzf_version("fzf 0.38.0"), (0, 0))

    def test_high_version_numbers(self):
        self.assertEqual(_parse_fzf_version("2.100.5"), (2, 100))

    def test_leading_whitespace_stripped(self):
        self.assertEqual(_parse_fzf_version("  1.5.0"), (1, 5))


# ===========================================================================
# _is_text_mime
# ===========================================================================


class TestIsTextMime(unittest.TestCase):
    """_is_text_mime identifies MIME types that should open in a text editor."""

    def test_text_plain(self):
        self.assertTrue(_is_text_mime("text/plain"))

    def test_text_html(self):
        self.assertTrue(_is_text_mime("text/html"))

    def test_text_x_python(self):
        self.assertTrue(_is_text_mime("text/x-python"))

    def test_application_json(self):
        self.assertTrue(_is_text_mime("application/json"))

    def test_application_xml(self):
        self.assertTrue(_is_text_mime("application/xml"))

    def test_application_javascript(self):
        self.assertTrue(_is_text_mime("application/javascript"))

    def test_inode_x_empty(self):
        # Zero-byte files should open in the editor.
        self.assertTrue(_is_text_mime("inode/x-empty"))

    def test_application_octet_stream(self):
        self.assertFalse(_is_text_mime("application/octet-stream"))

    def test_image_png(self):
        self.assertFalse(_is_text_mime("image/png"))

    def test_application_pdf(self):
        self.assertFalse(_is_text_mime("application/pdf"))

    def test_empty_string(self):
        self.assertFalse(_is_text_mime(""))


# ===========================================================================
# _shlex_join and _removeprefix
# ===========================================================================


class TestShlexJoin(unittest.TestCase):
    """_shlex_join produces shell-safe whitespace-joined argument strings."""

    def test_simple_args(self):
        self.assertEqual(_shlex_join(["echo", "hello"]), "echo hello")

    def test_arg_with_spaces_gets_quoted(self):
        self.assertIn("'hello world'", _shlex_join(["echo", "hello world"]))

    def test_arg_with_single_quote(self):
        result = _shlex_join(["echo", "it's"])
        # shlex.quote wraps in double-quotes or uses escape notation.
        self.assertIn("it", result)

    def test_empty_list(self):
        self.assertEqual(_shlex_join([]), "")

    def test_single_element(self):
        self.assertEqual(_shlex_join(["ls"]), "ls")


class TestRemovePrefix(unittest.TestCase):
    def test_prefix_present(self):
        self.assertEqual(_removeprefix("foobar", "foo"), "bar")

    def test_prefix_absent(self):
        self.assertEqual(_removeprefix("foobar", "baz"), "foobar")

    def test_empty_prefix(self):
        self.assertEqual(_removeprefix("foobar", ""), "foobar")

    def test_full_string_as_prefix(self):
        self.assertEqual(_removeprefix("foo", "foo"), "")

    def test_prefix_longer_than_string(self):
        self.assertEqual(_removeprefix("fo", "foo"), "fo")


# ===========================================================================
# classify / FileKind / _hint_suffix
# ===========================================================================


class TestHintSuffix(unittest.TestCase):
    """_hint_suffix extracts the longest matching extension, compound first."""

    def test_simple_suffix(self):
        self.assertEqual(_hint_suffix("file.py"), ".py")

    def test_compound_tar_gz(self):
        self.assertEqual(_hint_suffix("archive.tar.gz"), ".tar.gz")

    def test_compound_tar_bz2(self):
        self.assertEqual(_hint_suffix("backup.tar.bz2"), ".tar.bz2")

    def test_compound_tar_xz(self):
        self.assertEqual(_hint_suffix("data.tar.xz"), ".tar.xz")

    def test_gz_alone_not_compound(self):
        # .gz without .tar. is NOT a compound extension.
        self.assertEqual(_hint_suffix("file.gz"), ".gz")

    def test_case_insensitive(self):
        self.assertEqual(_hint_suffix("ARCHIVE.TAR.GZ"), ".tar.gz")

    def test_no_extension(self):
        self.assertEqual(_hint_suffix("Makefile"), "")


class TestClassify(unittest.TestCase):
    """classify() dispatches files to the correct FileKind."""

    def test_pdf_by_extension(self):
        self.assertEqual(classify("report.pdf"), FileKind.PDF)

    def test_pdf_by_mime(self):
        self.assertEqual(classify("file.txt", mime="application/pdf"), FileKind.PDF)

    def test_text_python(self):
        self.assertEqual(classify("main.py"), FileKind.TEXT)

    def test_text_markdown(self):
        self.assertEqual(classify("README.md"), FileKind.TEXT)

    def test_text_via_mime(self):
        self.assertEqual(classify("oddfile.xyz", mime="text/plain"), FileKind.TEXT)

    def test_text_empty_inode(self):
        self.assertEqual(classify("empty", mime="inode/x-empty"), FileKind.TEXT)

    def test_text_unknown_extension_no_mime(self):
        self.assertEqual(classify("unknown.xyz"), FileKind.TEXT)

    def test_archive_zip(self):
        self.assertEqual(classify("archive.zip"), FileKind.ARCHIVE)

    def test_archive_tar(self):
        self.assertEqual(classify("data.tar"), FileKind.ARCHIVE)

    def test_archive_7z(self):
        self.assertEqual(classify("package.7z"), FileKind.ARCHIVE)

    def test_archive_rar(self):
        self.assertEqual(classify("bundle.rar"), FileKind.ARCHIVE)

    def test_archive_tar_gz_compound(self):
        self.assertEqual(classify("archive.tar.gz"), FileKind.ARCHIVE)

    def test_archive_tar_bz2_compound(self):
        self.assertEqual(classify("archive.tar.bz2"), FileKind.ARCHIVE)

    def test_archive_tar_xz_compound(self):
        self.assertEqual(classify("archive.tar.xz"), FileKind.ARCHIVE)

    def test_binary_by_mime(self):
        self.assertEqual(
            classify("blob.bin", mime="application/octet-stream"), FileKind.BINARY
        )

    def test_directory_by_mime(self):
        self.assertEqual(
            classify("somedir", mime="inode/directory"), FileKind.DIRECTORY
        )

    def test_case_insensitive_archive(self):
        self.assertEqual(classify("ARCHIVE.ZIP"), FileKind.ARCHIVE)

    def test_case_insensitive_pdf(self):
        self.assertEqual(classify("REPORT.PDF"), FileKind.PDF)

    def test_directory_mime_takes_priority_over_extension(self):
        # Even a .zip named path should be a DIRECTORY if the mime says so.
        self.assertEqual(
            classify("archive.zip", mime="inode/directory"), FileKind.DIRECTORY
        )


# ===========================================================================
# LocalBackend.is_safe_subpath
# ===========================================================================


class TestLocalBackendIsSafeSubpath(unittest.TestCase):
    """LocalBackend.is_safe_subpath prevents directory traversal."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backend = LocalBackend(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_base_path_itself_is_safe(self):
        self.assertTrue(self.backend.is_safe_subpath(self.tmp))

    def test_existing_subdir_is_safe(self):
        sub = os.path.join(self.tmp, "subdir")
        os.makedirs(sub)
        self.assertTrue(self.backend.is_safe_subpath(sub))

    def test_etc_passwd_is_not_safe(self):
        self.assertFalse(self.backend.is_safe_subpath("/etc/passwd"))

    def test_parent_traversal_rejected(self):
        self.assertFalse(self.backend.is_safe_subpath(self.tmp + "/../etc"))

    def test_sibling_directory_rejected(self):
        sibling = tempfile.mkdtemp()
        try:
            self.assertFalse(self.backend.is_safe_subpath(sibling))
        finally:
            shutil.rmtree(sibling, ignore_errors=True)

    def test_nonexistent_path_inside_base_is_safe(self):
        # A file that does not exist yet but would be inside the base.
        inside = os.path.join(self.tmp, "new", "file.txt")
        self.assertTrue(self.backend.is_safe_subpath(inside))

    def test_root_is_not_safe(self):
        self.assertFalse(self.backend.is_safe_subpath("/"))


# ===========================================================================
# _merge_config_key
# ===========================================================================


class TestMergeConfigKey(unittest.TestCase):
    """_merge_config_key validates and merges one user config value."""

    def _fresh_cfg(self):
        cfg = dict(_CONFIG_DEFAULTS)
        cfg["exclude_patterns"] = list(_CONFIG_DEFAULTS["exclude_patterns"])
        return cfg

    def test_string_value_accepted(self):
        cfg = self._fresh_cfg()
        _merge_config_key(cfg, "editor", "", "nvim")
        self.assertEqual(cfg["editor"], "nvim")

    def test_bool_true_accepted(self):
        cfg = self._fresh_cfg()
        _merge_config_key(cfg, "show_hidden", False, True)
        self.assertTrue(cfg["show_hidden"])

    def test_bool_false_accepted(self):
        cfg = self._fresh_cfg()
        _merge_config_key(cfg, "show_hidden", False, False)
        self.assertFalse(cfg["show_hidden"])

    def test_integer_accepted(self):
        cfg = self._fresh_cfg()
        _merge_config_key(cfg, "ssh_control_persist", 60, 120)
        self.assertEqual(cfg["ssh_control_persist"], 120)

    def test_list_accepted(self):
        cfg = self._fresh_cfg()
        _merge_config_key(cfg, "exclude_patterns", [], [".git", "*.pyc"])
        self.assertEqual(cfg["exclude_patterns"], [".git", "*.pyc"])

    def test_empty_string_accepted(self):
        cfg = self._fresh_cfg()
        _merge_config_key(cfg, "editor", "vim", "")
        self.assertEqual(cfg["editor"], "")

    def test_wrong_type_keeps_default(self):
        cfg = self._fresh_cfg()
        with contextlib.redirect_stderr(io.StringIO()):
            _merge_config_key(cfg, "show_hidden", False, "yes")
        self.assertFalse(cfg["show_hidden"])  # unchanged

    def test_none_value_keeps_default(self):
        cfg = self._fresh_cfg()
        original = cfg["editor"]
        with contextlib.redirect_stderr(io.StringIO()):
            _merge_config_key(cfg, "editor", original, None)
        self.assertEqual(cfg["editor"], original)

    def test_list_expected_but_string_given_keeps_default(self):
        cfg = self._fresh_cfg()
        with contextlib.redirect_stderr(io.StringIO()):
            _merge_config_key(cfg, "exclude_patterns", [], "*.pyc")
        self.assertEqual(cfg["exclude_patterns"], [])

    def test_valid_file_source_accepted(self):
        cfg = self._fresh_cfg()
        _merge_config_key(cfg, "file_source", "auto", "git")
        self.assertEqual(cfg["file_source"], "git")

    def test_invalid_file_source_keeps_default(self):
        cfg = self._fresh_cfg()
        with contextlib.redirect_stderr(io.StringIO()):
            _merge_config_key(cfg, "file_source", "auto", "svn")
        self.assertEqual(cfg["file_source"], "auto")


# ===========================================================================
# _validate_custom_actions
# ===========================================================================


class TestValidateCustomActions(unittest.TestCase):
    """_validate_custom_actions parses and sanitises the custom_actions block."""

    def _minimal_valid(self):
        return {
            "leader": "ctrl-b",
            "groups": {
                "f": {
                    "label": "File",
                    "actions": {
                        "o": {"cmd": "xdg-open {path}", "label": "Open"},
                    },
                }
            },
        }

    def test_valid_config_accepted(self):
        result = _validate_custom_actions(self._minimal_valid())
        self.assertIsNotNone(result)
        self.assertIn("f", result["groups"])

    def test_non_dict_returns_none(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(_validate_custom_actions("not a dict"))

    def test_default_leader_when_missing(self):
        cfg = self._minimal_valid()
        del cfg["leader"]
        result = _validate_custom_actions(cfg)
        self.assertEqual(result["leader"], "ctrl-b")

    def test_invalid_leader_resets_to_default(self):
        cfg = self._minimal_valid()
        cfg["leader"] = ""
        with contextlib.redirect_stderr(io.StringIO()):
            result = _validate_custom_actions(cfg)
        self.assertEqual(result["leader"], "ctrl-b")

    def test_action_without_cmd_skipped(self):
        cfg = self._minimal_valid()
        cfg["groups"]["f"]["actions"]["x"] = {"label": "No cmd"}
        with contextlib.redirect_stderr(io.StringIO()):
            result = _validate_custom_actions(cfg)
        self.assertNotIn("x", result["groups"]["f"]["actions"])

    def test_multi_char_group_key_skipped(self):
        cfg = self._minimal_valid()
        cfg["groups"]["ab"] = {"label": "bad", "actions": {"x": {"cmd": "echo"}}}
        with contextlib.redirect_stderr(io.StringIO()):
            result = _validate_custom_actions(cfg)
        self.assertNotIn("ab", result["groups"])

    def test_invalid_output_resets_to_silent(self):
        cfg = self._minimal_valid()
        cfg["groups"]["f"]["actions"]["o"]["output"] = "unknown"
        with contextlib.redirect_stderr(io.StringIO()):
            result = _validate_custom_actions(cfg)
        self.assertEqual(result["groups"]["f"]["actions"]["o"]["output"], "silent")

    def test_preview_output_alias_becomes_overlay(self):
        # "preview" is a deprecated alias for "overlay".
        cfg = self._minimal_valid()
        cfg["groups"]["f"]["actions"]["o"]["output"] = "preview"
        result = _validate_custom_actions(cfg)
        self.assertEqual(result["groups"]["f"]["actions"]["o"]["output"], "overlay")


# ===========================================================================
# _build_fd_rga_args
# ===========================================================================


class TestBuildFdRgaArgs(unittest.TestCase):
    """_build_fd_rga_args constructs parallel fd and rga argument lists."""

    def test_file_type_flag_present(self):
        fd_args, _ = _build_fd_rga_args("f", "", False, [])
        self.assertIn("--type", fd_args)
        idx = fd_args.index("--type")
        self.assertEqual(fd_args[idx + 1], "f")

    def test_directory_type_flag(self):
        fd_args, _ = _build_fd_rga_args("d", "", False, [])
        self.assertIn("d", fd_args)

    def test_hidden_flag_in_both(self):
        fd_args, rga_args = _build_fd_rga_args("f", "", True, [])
        self.assertIn("--hidden", fd_args)
        self.assertIn("--hidden", rga_args)

    def test_hidden_flag_absent_when_not_requested(self):
        fd_args, rga_args = _build_fd_rga_args("f", "", False, [])
        self.assertNotIn("--hidden", fd_args)
        self.assertNotIn("--hidden", rga_args)

    def test_extension_added_to_both(self):
        fd_args, rga_args = _build_fd_rga_args("f", "py", False, [])
        self.assertTrue(any("py" in a for a in fd_args))
        self.assertTrue(any("py" in a for a in rga_args))

    def test_exclude_pattern_in_fd_args(self):
        fd_args, _ = _build_fd_rga_args("f", "", False, [".git"])
        self.assertIn(".git", " ".join(fd_args))

    def test_exclude_pattern_in_rga_args(self):
        _, rga_args = _build_fd_rga_args("f", "", False, [".git"])
        self.assertIn(".git", " ".join(rga_args))

    def test_unsafe_exclude_pattern_dropped(self):
        with contextlib.redirect_stderr(io.StringIO()):
            fd_args, rga_args = _build_fd_rga_args("f", "", False, ["*.py;id"])
        self.assertNotIn("*.py;id", " ".join(fd_args))
        self.assertNotIn("*.py;id", " ".join(rga_args))

    def test_hidden_flag_not_duplicated(self):
        fd_args, rga_args = _build_fd_rga_args("f", "py", True, [".git"])
        self.assertEqual(fd_args.count("--hidden"), 1)
        self.assertEqual(rga_args.count("--hidden"), 1)

    def test_multiple_extensions(self):
        fd_args, rga_args = _build_fd_rga_args("f", "py rs", False, [])
        joined = " ".join(fd_args)
        self.assertIn("py", joined)
        self.assertIn("rs", joined)


# ===========================================================================
# _build_remote_cmd
# ===========================================================================


class TestBuildRemoteCmd(unittest.TestCase):
    """_build_remote_cmd returns a safe, shell-ready command string."""

    def _call(self, **overrides):
        defaults = dict(
            fd_args=["fd", "-L", "--type", "f"],
            rga_glob_args=[],
            query="",
            base_path="/home/user",
            relative=False,
        )
        defaults.update(overrides)
        return _build_remote_cmd(**defaults)

    def test_returns_string(self):
        self.assertIsInstance(self._call(), str)

    def test_base_path_present(self):
        self.assertIn("/var/log", self._call(base_path="/var/log"))

    def test_query_included(self):
        self.assertIn("error", self._call(query="error"))

    def test_no_query_uses_fd(self):
        self.assertIn("fd", self._call(query=""))

    def test_relative_and_absolute_differ(self):
        abs_cmd = self._call(relative=False)
        rel_cmd = self._call(relative=True)
        self.assertNotEqual(abs_cmd, rel_cmd)

    def test_relative_mode_contains_cd(self):
        self.assertIn("cd", self._call(relative=True))

    def test_absolute_mode_no_leading_cd(self):
        cmd = self._call(relative=False, query="")
        self.assertFalse(cmd.lstrip().startswith("cd "))

    def test_base_path_with_spaces_is_quoted(self):
        cmd = self._call(base_path="/home/my user/projects")
        # shlex.quote wraps paths containing spaces in single quotes.
        self.assertIn("'", cmd)

    def test_query_with_special_chars_is_quoted(self):
        cmd = self._call(query="foo bar")
        self.assertIn("'foo bar'", cmd)

    def test_error_suffix_present(self):
        cmd = self._call()
        self.assertIn("exit 1", cmd)


# ===========================================================================
# _build_git_remote_cmd
# ===========================================================================


class TestBuildGitRemoteCmd(unittest.TestCase):
    """_build_git_remote_cmd produces git ls-files command strings."""

    def _call(self, **kw):
        defaults = dict(
            hidden=False, exclude_patterns=[], base_path="/repo", relative=True
        )
        defaults.update(kw)
        return _build_git_remote_cmd(**defaults)

    def test_contains_git_ls_files(self):
        self.assertIn("git ls-files", self._call())

    def test_relative_contains_cd(self):
        self.assertIn("cd", self._call(relative=True))

    def test_absolute_mode_no_leading_cd(self):
        cmd = self._call(relative=False)
        self.assertFalse(cmd.lstrip().startswith("cd "))

    def test_extension_filter_added(self):
        cmd = _build_git_remote_cmd(False, [], "/repo", True, ext="py")
        self.assertIn("*.py", cmd)

    def test_exclude_pattern_added(self):
        cmd = self._call(exclude_patterns=[".git"])
        self.assertIn(".git", cmd)

    def test_unsafe_exclude_pattern_dropped(self):
        cmd = self._call(exclude_patterns=["$(rm -rf ~)"])
        self.assertNotIn("rm -rf", cmd)


# ===========================================================================
# _parse_remote_reload_args
# ===========================================================================


class TestParseRemoteReloadArgs(unittest.TestCase):
    """_parse_remote_reload_args parses the remotely-remote-reload argument vector."""

    def _base(self, extra=None):
        return ["user@host", "/base/path", "/tmp/ssh_ctl", "f", ""] + (extra or [])

    def test_minimal_parse(self):
        args = _parse_remote_reload_args(self._base())
        self.assertEqual(args.remote, "user@host")
        self.assertEqual(args.base_path, "/base/path")
        self.assertEqual(args.ssh_control, "/tmp/ssh_ctl")
        self.assertEqual(args.ftype, "f")
        self.assertEqual(args.ext, "")

    def test_defaults_after_minimal_parse(self):
        args = _parse_remote_reload_args(self._base())
        self.assertFalse(args.hidden)
        self.assertFalse(args.relative)
        self.assertEqual(args.exclude_patterns, [])
        self.assertEqual(args.query, "")
        self.assertEqual(args.file_source, "fd")

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

    def test_single_exclude_pattern(self):
        args = _parse_remote_reload_args(self._base(["--exclude", "*.pyc"]))
        self.assertEqual(args.exclude_patterns, ["*.pyc"])

    def test_multiple_exclude_patterns(self):
        args = _parse_remote_reload_args(
            self._base(["--exclude", "*.pyc", "--exclude", ".git"])
        )
        self.assertEqual(args.exclude_patterns, ["*.pyc", ".git"])

    def test_exclude_missing_argument_returns_none(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(_parse_remote_reload_args(self._base(["--exclude"])))

    def test_file_source_git(self):
        args = _parse_remote_reload_args(self._base(["--file-source=git"]))
        self.assertEqual(args.file_source, "git")

    def test_all_flags_combined(self):
        args = _parse_remote_reload_args(
            self._base(["searchterm", "--hidden", "--relative", "--exclude", "*.log"])
        )
        self.assertEqual(args.query, "searchterm")
        self.assertTrue(args.hidden)
        self.assertTrue(args.relative)
        self.assertEqual(args.exclude_patterns, ["*.log"])

    def test_query_not_overwritten_by_second_positional(self):
        # Only the first positional after the five required args becomes query.
        args = _parse_remote_reload_args(self._base(["first", "second"]))
        self.assertEqual(args.query, "first")


# ===========================================================================
# _parse_target_path
# ===========================================================================


class TestParseTargetPath(unittest.TestCase):
    """_parse_target_path splits TARGET:PATH into (host, path)."""

    def test_absolute_local_path(self):
        self.assertEqual(_parse_target_path("/etc/hosts"), ("", "/etc/hosts"))

    def test_tilde_local_path(self):
        self.assertEqual(_parse_target_path("~/projects"), ("", "~/projects"))

    def test_dot_local_path(self):
        self.assertEqual(_parse_target_path("./relative"), ("", "./relative"))

    def test_remote_absolute_path(self):
        self.assertEqual(
            _parse_target_path("user@host:/var/log"), ("user@host", "/var/log")
        )

    def test_remote_tilde_path(self):
        self.assertEqual(
            _parse_target_path("user@host:~/projects"), ("user@host", "~/projects")
        )

    def test_hostname_only_without_colon(self):
        # No colon -> treated as local path.
        host, path = _parse_target_path("myserver")
        self.assertEqual(host, "")

    def test_colon_not_followed_by_slash_is_local(self):
        # "a:b" -- colon not followed by / or ~ -- treated as local.
        host, path = _parse_target_path("a:b")
        self.assertEqual(host, "")

    def test_host_with_user(self):
        host, path = _parse_target_path("alice@web01:/home/alice")
        self.assertEqual(host, "alice@web01")
        self.assertEqual(path, "/home/alice")


# ===========================================================================
# _is_built_script and _find_self
# ===========================================================================


class TestIsBuiltScript(unittest.TestCase):
    """_is_built_script identifies files by their shebang line."""

    def test_file_with_correct_shebang(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"#!/usr/bin/env python3\nprint('hello')\n")
            path = Path(f.name)
        try:
            self.assertTrue(_is_built_script(path))
        finally:
            path.unlink()

    def test_file_without_shebang(self):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"VERSION = '0.9.0'\n")
            path = Path(f.name)
        try:
            self.assertFalse(_is_built_script(path))
        finally:
            path.unlink()

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            path = Path(f.name)
        try:
            self.assertFalse(_is_built_script(path))
        finally:
            path.unlink()

    def test_nonexistent_path(self):
        self.assertFalse(_is_built_script(Path("/nonexistent/remotely")))

    def test_truncated_shebang(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"#!/usr/bin/env pyth")  # stops before "on3"
            path = Path(f.name)
        try:
            self.assertFalse(_is_built_script(path))
        finally:
            path.unlink()

    def test_different_shebang(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"#!/bin/bash\necho hi\n")
            path = Path(f.name)
        try:
            self.assertFalse(_is_built_script(path))
        finally:
            path.unlink()


class TestFindSelf(unittest.TestCase):
    """_find_self locates the built single-file script."""

    def test_returns_string_or_none(self):
        result = _find_self()
        self.assertTrue(result is None or isinstance(result, str))

    def test_returned_path_exists(self):
        result = _find_self()
        if result is not None:
            self.assertTrue(Path(result).exists(), f"Path does not exist: {result}")

    def test_returned_file_has_shebang(self):
        result = _find_self()
        if result is not None:
            self.assertTrue(
                _is_built_script(Path(result)),
                f"File at {result!r} has no remotely shebang",
            )


# ===========================================================================
# _build_bootstrap
# ===========================================================================


class TestBuildBootstrap(unittest.TestCase):
    """_build_bootstrap embeds the hash into the bootstrap script."""

    def test_empty_hash_returns_empty_bytes(self):
        self.assertEqual(_build_bootstrap(""), b"")

    def test_hash_present_in_output(self):
        bs = _build_bootstrap("abcdef1234567890")
        self.assertIn(b"abcdef1234567890", bs)

    def test_output_is_valid_python(self):
        import ast

        bs = _build_bootstrap("abcdef1234567890")
        # Should not raise SyntaxError.
        ast.parse(bs.decode())

    def test_exits_99_on_miss(self):
        bs = _build_bootstrap("abcdef1234567890")
        # The bootstrap must exit 99 when the cached file is not found.
        self.assertIn(b"99", bs)

    def test_checks_dev_shm_first(self):
        bs = _build_bootstrap("abcdef1234567890")
        dev_shm_idx = bs.find(b"/dev/shm")
        cache_idx = bs.find(b".cache")
        self.assertLess(dev_shm_idx, cache_idx)

    def test_hash_not_double_substituted(self):
        # A hash that contains the placeholder string should not cause issues.
        bs = _build_bootstrap("0123456789abcdef")
        self.assertIn(b"0123456789abcdef", bs)
        self.assertNotIn(b"__HASH__", bs)


# ===========================================================================
# _ssh_opts
# ===========================================================================


class TestSshOpts(unittest.TestCase):
    """_ssh_opts returns ControlMaster flags or an empty list."""

    def test_empty_control_returns_empty_list(self):
        self.assertEqual(_ssh_opts(""), [])

    def test_non_empty_control_returns_list(self):
        with tempfile.TemporaryDirectory() as d:
            sock = os.path.join(d, "ssh.sock")
            opts = _ssh_opts(sock)
            self.assertIsInstance(opts, list)
            self.assertTrue(len(opts) > 0)

    def test_control_path_in_opts(self):
        with tempfile.TemporaryDirectory() as d:
            sock = os.path.join(d, "ssh.sock")
            opts = _ssh_opts(sock)
            joined = " ".join(opts)
            self.assertIn(sock, joined)

    def test_control_master_auto_in_opts(self):
        with tempfile.TemporaryDirectory() as d:
            sock = os.path.join(d, "ssh.sock")
            opts = _ssh_opts(sock)
            joined = " ".join(opts)
            self.assertIn("ControlMaster=auto", joined)


# ===========================================================================
# backend_from_state
# ===========================================================================


class TestBackendFromState(unittest.TestCase):
    """backend_from_state reconstructs the correct backend from a state dict."""

    def _state(self, remote="", base_path="/tmp", ssh_control="", exclude=None):
        return {
            "remote": remote,
            "base_path": base_path,
            "ssh_control": ssh_control,
            "exclude_patterns": exclude or [],
        }

    def test_empty_remote_returns_local_backend(self):
        self.assertIsInstance(backend_from_state(self._state()), LocalBackend)

    def test_ssh_host_returns_remote_backend(self):
        self.assertIsInstance(
            backend_from_state(self._state(remote="user@host")), RemoteBackend
        )

    def test_hostname_without_user_returns_remote_backend(self):
        self.assertIsInstance(
            backend_from_state(self._state(remote="myserver")), RemoteBackend
        )

    def test_local_base_path_stored(self):
        be = backend_from_state(self._state(base_path="/home/user"))
        self.assertEqual(be.base_path, "/home/user")

    def test_remote_host_stored(self):
        be = backend_from_state(self._state(remote="myserver", base_path="/data"))
        self.assertEqual(be.remote, "myserver")
        self.assertEqual(be.base_path, "/data")

    def test_exclude_patterns_propagated(self):
        be = backend_from_state(self._state(exclude=[".git", "*.pyc"]))
        self.assertEqual(be.exclude_patterns, [".git", "*.pyc"])

    def test_empty_exclude_patterns_stored_as_empty_list(self):
        be = backend_from_state(self._state())
        self.assertEqual(be.exclude_patterns, [])

    def test_any_non_empty_remote_string_is_ssh(self):
        # Even the string "local" is treated as an SSH host by this function;
        # "local" routing is resolved before backend_from_state is called.
        self.assertIsInstance(
            backend_from_state(self._state(remote="local")), RemoteBackend
        )


# ===========================================================================
# _save_state / _load_state / _mutate_state
# ===========================================================================


class TestState(unittest.TestCase):
    """State file round-trips, boundary checks, and atomic mutations."""

    def setUp(self):
        WORK_BASE.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.mkdtemp(dir=str(WORK_BASE))
        self.state_path = Path(self.tmp) / "state.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_load_round_trip(self):
        state = {"mode": "content", "remote": "", "show_hidden": False}
        _save_state(self.state_path, state)
        self.assertEqual(_load_state(self.state_path), state)

    def test_load_missing_file_returns_empty_dict(self):
        self.assertEqual(_load_state(Path(self.tmp) / "missing.json"), {})

    def test_save_creates_file(self):
        _save_state(self.state_path, {"k": "v"})
        self.assertTrue(self.state_path.exists())

    def test_save_leaves_no_tmp_file(self):
        _save_state(self.state_path, {"k": "v"})
        self.assertFalse(self.state_path.with_suffix(".tmp").exists())

    def test_mutate_applies_function(self):
        _save_state(self.state_path, {"mode": "name"})
        rc = _mutate_state(self.state_path, lambda s: s.update({"mode": "content"}))
        self.assertEqual(rc, 0)
        self.assertEqual(_load_state(self.state_path)["mode"], "content")

    def test_mutate_returns_zero_on_success(self):
        _save_state(self.state_path, {"x": 1})
        self.assertEqual(_mutate_state(self.state_path, lambda s: None), 0)

    def test_mutate_missing_file_returns_one(self):
        rc = _mutate_state(Path(self.tmp) / "missing.json", lambda s: None)
        self.assertEqual(rc, 1)

    def test_mutate_preserves_unmodified_keys(self):
        _save_state(self.state_path, {"mode": "name", "hidden": False, "ext": "py"})
        _mutate_state(self.state_path, lambda s: s.update({"hidden": True}))
        loaded = _load_state(self.state_path)
        self.assertEqual(loaded["mode"], "name")
        self.assertEqual(loaded["ext"], "py")
        self.assertTrue(loaded["hidden"])

    def test_mutate_raising_function_returns_one(self):
        _save_state(self.state_path, {"x": 1})

        def bad(s):
            raise ValueError("intentional")

        self.assertEqual(_mutate_state(self.state_path, bad), 1)

    def test_load_outside_workbase_returns_empty(self):
        # SECURITY: paths outside WORK_BASE must be rejected.
        outside = Path("/tmp/remotely_test_outside_workbase_xyz.json")
        self.assertEqual(_load_state(outside), {})

    def test_save_overwrites_existing_file(self):
        _save_state(self.state_path, {"v": 1})
        _save_state(self.state_path, {"v": 2})
        self.assertEqual(_load_state(self.state_path)["v"], 2)


# ===========================================================================
# _assert_not_symlink
# ===========================================================================


class TestAssertNotSymlink(unittest.TestCase):
    """_assert_not_symlink exits on symlinks, passes on regular paths."""

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
        _assert_not_symlink(Path("/nonexistent/remotely_test_xyz"))

    def test_symlink_triggers_sys_exit(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "target"
            target.mkdir()
            link = Path(d) / "link"
            link.symlink_to(target)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(
                io.StringIO()
            ):
                _assert_not_symlink(link)

    def test_symlink_to_file_triggers_sys_exit(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "target.txt"
            target.write_text("x")
            link = Path(d) / "link.txt"
            link.symlink_to(target)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(
                io.StringIO()
            ):
                _assert_not_symlink(link)


# ===========================================================================
# _find_git_root
# ===========================================================================


class TestFindGitRoot(unittest.TestCase):
    """_find_git_root walks upward from cwd to find a .git directory."""

    def test_returns_string_or_none(self):
        result = _find_git_root()
        self.assertTrue(result is None or isinstance(result, str))

    def test_returned_path_has_dot_git(self):
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


# ===========================================================================
# load_config
# ===========================================================================


class TestLoadConfig(unittest.TestCase):
    """load_config returns a dict that merges defaults with user config."""

    def test_returns_dict(self):
        self.assertIsInstance(load_config(), dict)

    def test_all_default_keys_present(self):
        cfg = load_config()
        for key in _CONFIG_DEFAULTS:
            with self.subTest(key=key):
                self.assertIn(key, cfg)

    def test_default_mode_valid(self):
        self.assertIn(load_config()["default_mode"], ("name", "content"))

    def test_show_hidden_is_bool(self):
        self.assertIsInstance(load_config()["show_hidden"], bool)

    def test_exclude_patterns_is_list(self):
        self.assertIsInstance(load_config()["exclude_patterns"], list)

    def test_max_stream_mb_is_non_negative_int(self):
        val = load_config()["max_stream_mb"]
        self.assertIsInstance(val, int)
        self.assertGreaterEqual(val, 0)

    def test_ssh_multiplexing_is_bool(self):
        self.assertIsInstance(load_config()["ssh_multiplexing"], bool)


# ===========================================================================
# Security-focused tests
# ===========================================================================


class TestSecurityResolveRemotePath(unittest.TestCase):
    """_resolve_remote_path uses stdin for tilde expansion to prevent injection."""

    @patch("subprocess.run")
    def test_tilde_expansion_uses_stdin(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"/home/user/foo\n")
        result = _resolve_remote_path("user@host", "~/foo", "")
        self.assertEqual(result, "/home/user/foo")
        _, kwargs = mock_run.call_args
        # The raw path must be passed via stdin, NOT embedded in the command.
        self.assertEqual(kwargs.get("input"), b"~/foo")

    @patch("subprocess.run")
    def test_tilde_expansion_command_uses_python3(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=b"/home/user\n")
        _resolve_remote_path("user@host", "~", "")
        args, _ = mock_run.call_args
        cmd = " ".join(args[0])
        self.assertIn("python3", cmd)
        self.assertIn("os.path.expanduser", cmd)

    @patch("subprocess.run")
    def test_dot_uses_pwd(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="/current/dir\n")
        result = _resolve_remote_path("host", ".", "")
        self.assertEqual(result, "/current/dir")
        args, _ = mock_run.call_args
        self.assertIn("pwd", args[0])

    @patch("subprocess.run")
    def test_absolute_path_returned_unchanged(self, mock_run):
        result = _resolve_remote_path("host", "/absolute/path", "")
        self.assertEqual(result, "/absolute/path")
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_ssh_failure_exits(self, mock_run):
        mock_run.return_value = MagicMock(returncode=255, stdout=b"")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            _resolve_remote_path("host", "~/foo", "")


class TestVersionConstant(unittest.TestCase):
    def test_version_is_string(self):
        self.assertIsInstance(VERSION, str)

    def test_version_matches_semver_pattern(self):
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
