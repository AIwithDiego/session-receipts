# How the README images are made

- `cover.html` draws the README cover and the social preview (1280×640) from the trial-week numbers in `LEARNINGS.md` and a screenshot of the demo.
- `docs/how-it-works.svg` is plain SVG, edited by hand.
- `docs/assets/dashboard.png` and `receipt.png` are screenshots of the made-up demo (`python3 scripts/demo.py`); the receipt is `#s=demo005`.
- `docs/demo/` is the hosted demo: `python3 scripts/demo.py --pages` writes it, frozen so "7 days" counts from the build.

```bash
C="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
python3 scripts/demo.py --pages
"$C" --headless=new --hide-scrollbars --force-device-scale-factor=2 --window-size=1280,860 --allow-file-access-from-files \
  --virtual-time-budget=3000 --screenshot="$PWD/docs/assets/dashboard.png" "file://$PWD/demo/home/index.html"
"$C" --headless=new --hide-scrollbars --force-device-scale-factor=2 --window-size=1280,860 --allow-file-access-from-files \
  --virtual-time-budget=4000 --screenshot="$PWD/docs/assets/receipt.png" "file://$PWD/demo/home/index.html#s=demo005"
cd docs/src && "$C" --headless=new --hide-scrollbars --force-device-scale-factor=2 --window-size=1280,640 \
  --allow-file-access-from-files --screenshot="$PWD/../assets/cover.png" "file://$PWD/cover.html"
```
