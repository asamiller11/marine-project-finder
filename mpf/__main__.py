import warnings

# macOS's built-in Python 3.9 uses LibreSSL; urllib3 warns about it but works fine.
warnings.filterwarnings("ignore", message=".*urllib3 v2 only supports OpenSSL.*")

from mpf.cli import main  # noqa: E402

main()
