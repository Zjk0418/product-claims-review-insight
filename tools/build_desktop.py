"""Build the portable Windows application, then reject bundled user data or secrets."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _dist_root() -> Path:
    root = ROOT.resolve()
    dist = (root / "dist").resolve()
    if dist.parent != root or dist.name.lower() != "dist":
        raise RuntimeError("dist 目录指向当前项目以外，拒绝执行")
    return dist


def _validate_exe(exe: Path, *, must_exist: bool = True) -> Path:
    """Treat a release pointer as data, never as a command or shell expression."""
    dist = _dist_root()
    exe = exe.resolve()
    if (not exe.is_relative_to(dist) or exe.name != "ReviewInsight.exe"
            or exe.parent.name != "ReviewInsight"):
        raise RuntimeError("应用路径必须是 dist 内的 ReviewInsight/ReviewInsight.exe")
    relative = exe.relative_to(ROOT.resolve()).as_posix()
    # The ASCII launcher reads this line; keep shell metacharacters out of it.
    if not relative.isascii() or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/" for character in relative):
        raise RuntimeError("发布路径只能包含 ASCII 字母、数字、点、横线、下划线及目录分隔符")
    if must_exist and not exe.is_file():
        raise RuntimeError("当前应用指针指向不存在的可执行程序")
    return exe


def read_current_app() -> Path | None:
    pointer = _dist_root() / "current-app.txt"
    if pointer.is_symlink():
        raise RuntimeError("当前应用指针不能是符号链接")
    if not pointer.exists():
        return None
    try:
        lines = pointer.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        raise RuntimeError("当前应用指针必须是单行 ASCII 相对路径") from None
    if len(lines) != 1 or not lines[0] or lines[0] != lines[0].strip():
        raise RuntimeError("当前应用指针必须是单行 ASCII 相对路径")
    relative = Path(lines[0])
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise RuntimeError("当前应用指针不能包含绝对路径或上级目录")
    return _validate_exe(ROOT / relative)


def resolve_output_dir(output_dir: Path | str | None = None) -> Path:
    """Choose a release directory and reject unrelated output before PyInstaller."""
    dist = _dist_root()
    if output_dir is None:
        current = read_current_app()
        selected = current.parent.parent if current else dist
    else:
        selected = Path(output_dir)
        selected = selected if selected.is_absolute() else ROOT / selected
        selected = selected.resolve()
        if selected == dist:
            raise RuntimeError("--output-dir 必须是 dist 内的新子目录")
    selected = selected.resolve()
    if not selected.is_relative_to(dist):
        raise RuntimeError("输出目录超出当前项目的 dist，拒绝执行")
    bundle = selected / "ReviewInsight"
    exe = _validate_exe(bundle / "ReviewInsight.exe", must_exist=False)
    if exe.parent != bundle or bundle.resolve() != bundle:
        raise RuntimeError("输出目录中的应用文件不能重定向到其他目录")
    if selected.exists():
        if not selected.is_dir():
            raise RuntimeError("输出目录不是文件夹")
        if any(selected.iterdir()) and not exe.is_file():
            raise RuntimeError("输出目录包含未识别内容，拒绝覆盖")
    if bundle.exists() and not exe.is_file():
        raise RuntimeError("应用输出目录包含未识别内容，拒绝覆盖")
    return selected


def publish_current_app(exe: Path) -> None:
    """Atomically publish only a fully checked release's safe relative filename."""
    exe = _validate_exe(exe)
    dist = _dist_root()
    dist.mkdir(parents=True, exist_ok=True)
    pointer = dist / "current-app.txt"
    if pointer.is_symlink():
        raise RuntimeError("当前应用指针不能是符号链接")
    relative = exe.relative_to(ROOT.resolve()).as_posix()
    with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", newline="\n", dir=dist,
                                     prefix=".current-app-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(relative + "\n")
    try:
        os.replace(temporary, pointer)
    finally:
        if temporary.exists():
            temporary.unlink()


def refresh_latest_junction(bundle: Path) -> None:
    """把 dist/releases/latest 指向刚构建的版本（桌面快捷方式用它，避免写死版本号）。

    bundle 是包含 ReviewInsight.exe 的目录，例如 dist/releases/3.10.2/ReviewInsight。
    """
    dist = _dist_root()
    link = dist / "releases" / "latest"
    if link.exists() or link.is_symlink():
        try:
            link.unlink()          # 符号链接
        except OSError:
            os.rmdir(link)         # 目录联接：只删链接，不动目标目录
    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(Path(bundle).resolve().parent)],
            check=True, capture_output=True,
        )
    except Exception:
        # 快捷方式失效不应让构建失败
        pass


def development_keys() -> list[bytes]:
    """Used only to compare bytes locally; never return keys in logs or manifests."""
    keys = []
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            name, value = line.split("=", 1)
            value = value.strip().strip("\"'")
            if name.strip() in {"LLM_API_KEY", "OPENAI_API_KEY"} and value:
                keys.append(value.encode("utf-8"))
    return keys


def audit_bundle(bundle: Path, keys: list[bytes]) -> dict:
    files = [path for path in bundle.rglob("*") if path.is_file()]
    rejected = []
    secret_hits = []
    for path in bundle.rglob("*"):
        if not path.is_dir():
            continue
        relative = path.relative_to(bundle)
        parts = {part.lower() for part in relative.parts}
        dependency_runtime = relative.parts[:3] == ("_internal", "pythonnet", "runtime")
        if parts & {".cdp-edge-profile", ".cdp-chrome-profile"} or "runtime" in parts and not dependency_runtime:
            rejected.append(relative.as_posix() + "/")
    for path in files:
        relative = path.relative_to(bundle)
        parts = {part.lower() for part in relative.parts}
        # pythonnet/runtime contains required .NET libraries, not application data.
        dependency_runtime = relative.parts[:3] == ("_internal", "pythonnet", "runtime")
        if path.name.lower().startswith(".env") or parts & {".cdp-edge-profile", ".cdp-chrome-profile"} or (
            "runtime" in parts and not dependency_runtime
        ) or path.name == "api-settings.json":
            rejected.append(relative.as_posix())
        if keys:
            overlap = max(len(key) for key in keys)
            previous = b""
            with path.open("rb") as handle:
                while block := handle.read(1024 * 1024):
                    block = previous + block
                    if any(key in block for key in keys):
                        secret_hits.append(relative.as_posix())
                        break
                    previous = block[-overlap:]
    return {"status": "ok" if not rejected and not secret_hits else "failed", "file_count": len(files),
            "private_data_paths": rejected, "secret_hit_files": secret_hits,
            "checked_development_key_count": len(keys),
            "note": "包内数据只来自spec白名单；开发密钥只在内存中进行字节匹配，未写入报告。"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true", help="只检查现有输出")
    parser.add_argument("--skip-zip", action="store_true")
    parser.add_argument("--output-dir", type=Path, help="dist 内的独立版本目录（相对项目根目录或绝对路径）")
    args = parser.parse_args(argv)
    dist = resolve_output_dir(args.output_dir)
    bundle = dist / "ReviewInsight"
    work = ROOT / "build/pyinstaller"
    # PyInstaller can replace its build output; prevent use against unrelated paths.
    if not bundle.resolve().is_relative_to(ROOT) or not work.resolve().is_relative_to(ROOT):
        raise RuntimeError("构建路径超出当前项目，拒绝执行")
    if bundle.exists() and not (bundle / "ReviewInsight.exe").is_file():
        raise RuntimeError("输出目录包含未识别内容，拒绝覆盖")
    if not args.skip_build:
        command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(dist),
                   "--workpath", str(work), str(ROOT / "ReviewInsight.spec")]
        completed = subprocess.run(command, cwd=ROOT)
        if completed.returncode:
            return completed.returncode
    if not (bundle / "ReviewInsight.exe").exists():
        raise RuntimeError("尚未生成可执行程序")
    guide = ROOT / "桌面应用部署说明.md"
    if guide.exists():
        shutil.copy2(guide, bundle / "使用说明.md")
    report = audit_bundle(bundle, development_keys())
    (dist / "package-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if report["status"] != "ok":
        print(json.dumps(report, ensure_ascii=False))
        return 1
    with tempfile.TemporaryDirectory(prefix="review-package-smoke-") as tmp:
        target = Path(tmp) / "result.json"
        process = subprocess.run([str(bundle / "ReviewInsight.exe"), "--self-test", "--self-test-output", str(target)],
                                 cwd=bundle, timeout=180)
        smoke = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {"status": "failed", "reason": "no_self_test_result"}
        smoke["exit_code"] = process.returncode
    (dist / "package-smoke.json").write_text(json.dumps(smoke, ensure_ascii=False, indent=2), encoding="utf-8")
    if process.returncode or smoke["status"] != "ok":
        print(json.dumps(smoke, ensure_ascii=False))
        return 1
    # Also verify the executable did not create reports or data in its install folder.
    report = audit_bundle(bundle, development_keys())
    report["post_smoke_audit"] = report["status"]
    (dist / "package-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if report["status"] != "ok":
        print(json.dumps(report, ensure_ascii=False))
        return 1
    if not args.skip_zip:
        destination = dist / "ReviewInsight-portable.zip"
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in bundle.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(dist))
        report["portable_zip"] = {"file": destination.name, "bytes": destination.stat().st_size,
                                  "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
        (dist / "package-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        # Publishing before any of the above stages finishes could strand the
        # launcher on a failed build. --skip-zip remains an audit-only operation.
        publish_current_app(bundle / "ReviewInsight.exe")
        refresh_latest_junction(bundle)
    print(json.dumps({"package": str(bundle / "ReviewInsight.exe"), "audit": report["status"],
                      "smoke": smoke["status"], "files": report["file_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
