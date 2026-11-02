# PyInstaller one-directory build. Data inputs are deliberately allow-listed.
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project = Path(SPEC).resolve().parent
sys.path.insert(0, str(project / "app"))
datas = [(str(project / "app/static"), "app/static"),
         (str(project / "models/bge-small-zh-v1.5"), "models/bge-small-zh-v1.5"),
         (str(project / "taobao-jd-review-cdp/taobao-tmall/taobao-tmall-review-exporter/scripts/tmall_rate_auto.py"),
          "taobao-jd-review-cdp/taobao-tmall/taobao-tmall-review-exporter/scripts")]
if (project / "app/assets").exists():
    datas.append((str(project / "app/assets"), "app/assets"))
datas += collect_data_files("rapidocr_onnxruntime")
datas += collect_data_files("reportlab")
hidden = ["server", "collector", "cdp_tabs", "product", "importer", "job_store", "intervention",
          "api_settings", "resources", "reporting", "webview.platforms.edgechromium", "pandas", "openpyxl",
          "reportlab", "onnxruntime", "tokenizers", "sklearn", "rapidocr_onnxruntime"]
hidden += ["reportlab.pdfbase.cidfonts", "reportlab.pdfbase._cidfontdata", "reportlab.pdfbase.ttfonts"]
hidden += [name for name in collect_submodules("analysis") if ".tests" not in name]
hidden += [path.stem for path in (project / "app").glob("*.py") if path.stem != "__init__"]
a = Analysis([str(project / "desktop_launcher.py")], pathex=[str(project), str(project / "app")],
             binaries=[], datas=datas, hiddenimports=hidden,
             excludes=["pytest", "IPython", "jupyter", "torch", "tensorflow", "pyarrow",
                       "pandas.tests", "numpy.tests", "scipy.tests", "sklearn.tests", "matplotlib.tests"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ReviewInsight", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ReviewInsight")
