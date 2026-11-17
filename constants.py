"""插件全局默认参数、文件限制与自动依赖映射常量。"""

import re

DEFAULT_EXEC_TIMEOUT = 60
DEFAULT_OUTPUT_LIMIT = 2000
DEFAULT_PROXY = ""
DEFAULT_TEMPLATE = ""
DEFAULT_SANDBOX_SCOPE = "session"
DEFAULT_UPLOAD_DIR = "/home/user/uploads"
DEFAULT_WORK_DIR = "/home/user"
DEFAULT_EXPORT_DIRNAME = "exports"
MAX_RESULT_LIMIT = 20000
MAX_SESSION_FILE_COUNT = 5
MAX_GENERATED_FILE_CANDIDATES = 3
MAX_IMAGES_PER_EXECUTION = 5
PENDING_UPLOAD_TTL_SECONDS = 1800
DEFAULT_MAX_RETURN_FILE_SIZE_MB = 5
MAX_RETURN_FILE_SIZE_MB = 50
DEFAULT_MAX_UPLOAD_FILE_SIZE_MB = 20
DEFAULT_FILE_RETENTION_HOURS = 24
DEFAULT_SESSION_RETENTION_HOURS = 12
DEFAULT_SANDBOX_TIMEOUT = 600
DEFAULT_DUPLICATE_EXEC_WINDOW_SECONDS = 10
DEFAULT_MAX_RUNNING_SANDBOXES = 3
DEFAULT_MAX_QUEUE_SIZE = 10
DEFAULT_QUEUE_WAIT_TIMEOUT = 15
DEFAULT_IDLE_PAUSE_SECONDS = 30
DEFAULT_PAUSED_RETENTION_HOURS = 12
SANDBOX_API_TIMEOUT = 20
PLUGIN_NAME = "astrbot_plugin_e2b_sandbox"
SANDBOX_PATH_PATTERN = re.compile(r"(/home/user(?:/[\w\-. \u4e00-\u9fff]+)+)")

# 绘图相关模块与符号集合，用于按需触发 matplotlib 中文字体与图像回传引导
PLOT_MODULES = frozenset({"matplotlib", "pylab", "seaborn", "wordcloud", "plotly"})
PLOT_IDENTIFIERS = frozenset({
    "plt", "sns", "WordCloud", "savefig", "figure", "subplots",
    "imshow", "matshow", "boxplot", "hist", "scatter",
})

IMPORT_PACKAGE_MAP = {
    "PIL": "Pillow",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dotenv": "python-dotenv",
    "jieba": "jieba",
    "matplotlib": "matplotlib",
    "numpy": "numpy",
    "openpyxl": "openpyxl",
    "pandas": "pandas",
    "requests": "requests",
    "seaborn": "seaborn",
    "sklearn": "scikit-learn",
    "wordcloud": "wordcloud",
}
