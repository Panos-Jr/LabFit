# LabFit

A plot-and-fit tool for the Y1 Physics Lab: paste your data, get a fit with proper uncertainties, and export a lab-report-ready plot.

Originally created by Wolfgang Theis (2019). This version is a complete rewrite with an Excel-style spreadsheet, formulas, a column calculator, richer fit diagnostics and cross-platform builds.

## Download and run

You do **not** need Python. Download the build for your system from the [**Releases**](../../releases) page, extract it, and run it.

| System | File | How to run |
| :--- | :--- | :--- |
| **Windows** | `LabFit-Windows.zip` | Extract, double-click `LabFit.exe`. |
| **Windows** (faster start) | `LabFit-Windows-folder.zip` | Extract the **whole folder** (don't run from inside the zip), double-click `LabFit.exe` inside it. |
| **macOS** (Apple Silicon: M1 or later) | `LabFit-Mac-M1.zip` | Extract, open the `LabFit` folder, run `LabFit`. See [macOS notes](#macos). |
| **Linux** | `LabFit-Linux.zip` | Extract, open the `LabFit` folder, mark `LabFit` as executable ("Allow executing file as program", or `chmod +x LabFit/LabFit`), run it. |

**Which Windows version?** The single `.exe` unpacks itself to a temporary folder every time it starts, so it takes about 5 s to open in our tests. The folder version opens in under 2 s (the very first launch can be slower while Windows scans the files). If start-up speed matters to you, use the folder version.

Intel Macs are not supported by the current macOS build.

## Quick start

1. **Enter data.** Paste from Excel/Sheets (`Ctrl+V`) or type into the table. A header row is skipped automatically.
2. **Check the column roles.** By default **A = X**, **B = Y**, **C = ΔY** (the uncertainty on Y). Change them with the X / Y / ΔX / ΔY boxes under the table. The column headers show which role each column has.
3. Press **Plot data**.
4. Pick a **Model**, optionally type start values (leave blank for automatic), and press **Fit** (`Ctrl+Enter`).
5. Read the **fit report** (copy it with *Copy report*) and the residual plot under the main plot.
6. Set the title, axis labels and your signature, then save the plot with **File → Save plot** (`Ctrl+S`, PNG / PDF / SVG at 300 dpi) or print it.

Changing *scaled residuals*, *log X* or *log Y* redraws instantly without refitting.

## Features

- **Spreadsheet.** 6 columns (A–F), up to 200,000 rows, undo/redo for everything, Excel-style copy/paste/fill, and a status-bar summary (count, sum, mean, standard deviation, min, max) for any selected cells.
- **Formulas.** Type `=A2*2` or `=1/sqrt(B3)` straight into a cell (see [Formulas](#formulas)).
- **Column calculator.** Apply a formula down a whole column in one go, e.g. compute 1/√V to linearise your data.
- **12 fit models** with automatic starting values and multi-start fitting for oscillating and peaked models (see [Fit models](#fit-models)).
- **Uncertainties handled properly.** ΔY gives a weighted fit; adding ΔX switches to orthogonal distance regression; with no uncertainties the errors can be inferred from the scatter.
- **Diagnostics.** χ², reduced χ² (χ²/ν), p-value, R², RMS residual, parameter uncertainties, and plain-language warnings when something looks wrong.
- **Residual plot.** Colour-coded by size: green ≤ 1σ, orange 1–3σ, red ≥ 3σ.
- **Fitting never freezes the window.** It runs in the background and can be cancelled (`Ctrl+.`).
- **Files.** Open and save CSV, save the plot as PNG / PDF / SVG, print.

## The data table

| Action | How |
| :--- | :--- |
| Paste | `Ctrl+V` into any cell; the block lands with its top-left at the selected cell. Tab, semicolon, comma or space separated all work. Decimal commas (`1,5`) are accepted. |
| Uncertainties | A blank ΔX/ΔY cell **copies the value from the row above**, so you can enter one error and leave the rest empty. |
| Errors | Cells that can't be evaluated show **`#ERR`** in red; hover to see why. They are listed in the status line under the table. An error in an X or Y cell drops that row from the plot and fit; an error in a ΔX/ΔY cell is treated as missing. |
| Rows with only X or only Y | Ignored (and mentioned in the status line). |
| Clear all | **Clear data** button or *Edit → Clear all data*. This can be undone. |

Uncertainties must be greater than zero. If some rows have a ΔY and others don't, LabFit tells you which rows are missing it instead of guessing.

## Formulas

A cell that starts with `=` is a formula. If you type a bare expression such as `1/sqrt(A2)`, LabFit adds the `=` for you.

**Cell references:** `A1`–`F…`, upper or lower case. `$A$1` style absolute references work too.

**Operators:** `+  -  *  /  ^` (or `**` for powers), `%` (remainder), `//` (floor division), brackets.

**Functions:**
`sqrt`, `abs`, `exp`, `ln`, `log10`, `log2`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `atan2`, `sinh`, `cosh`, `tanh`, `floor`, `ceil`, `round`, `min`, `max`, `pow`, `rad`, `deg`, `sign`

> `log` is the **natural** logarithm (same as `ln`). Use `log10` for base 10. Trigonometric functions work in **radians**; use `rad(...)` to convert from degrees.

**Constants:** `pi`, `e`, `tau`

**Examples** (assuming X is in column A and Y in column B):

| Formula | Meaning |
| :--- | :--- |
| `=A1 * 2` | Double the value in A1 |
| `1/sqrt(A2)` | Inverse square root |
| `=(A2+A3)/2` | Average of two cells |
| `=sin(pi/4)` | A constant expression |
| `=$A$1 * B2` | Always multiply by cell A1 |
| `=B2/A2^2` | Combine columns |

Circular references, division by zero, and invalid arithmetic (e.g. `sqrt(-1)`) show `#ERR` rather than crashing. Only numbers and the functions above are allowed; nothing else is ever executed.

### Column calculator ("Calculate a column")

Writes a formula down a whole column:

1. Choose the column to **put the result in**.
2. Type the formula **for the first row**, e.g. `=1/sqrt(A2)`. References shift down row by row, like filling down in Excel. Or pick a **quick template** (1/√A, √A, A², 1/A, 1/A², ln A, log10 A, exp A, |A|, A+B, A−B, A×B, A÷B) and choose which columns are A and B.
3. Press **Fill column**.

A live preview shows the first row's value. LabFit checks the first row works before writing anything, asks before overwriting existing cells, and the whole fill is a single undo step. It stops at the last row that has data in the columns your formula uses.

## Fit models

| Model | Equation | Parameters |
| :--- | :--- | :--- |
| constant | y = a | a |
| proportional | y = a·x | a |
| linear | y = a·x + b | a, b |
| quadratic | y = a·x² + b·x + c | a, b, c |
| cubic | y = a·x³ + b·x² + c·x + d | a, b, c, d |
| cubic (odd powers) | y = a·x³ + b·x | a, b |
| power | y = a·\|x\|^p | a, p |
| exponential | y = a·exp(b·x) | a, b |
| Gaussian | y = a·exp(−(x−b)² / 2c²) | a, b, c |
| resonance (Lorentzian) | y = a·(Δx/2)² / [(x−x₀)² + (Δx/2)²] | a, x₀, Δx |
| cosine | y = a·cos(b·x + c) | a, b, c |
| \|cosine\| | y = a·\|cos(b·x + c)\| | a, b, c |

Start values are optional; blank boxes are filled with an automatic estimate. After a fit, the boxes show the fitted values, so you can nudge them and refit. **Plot model** draws the curve for the current start values without fitting, which is handy for finding a good starting point.

## Understanding the results

| You supply | LabFit does |
| :--- | :--- |
| ΔY for every point | Weighted least squares. Reports χ², χ²/ν, p-value. |
| ΔY and ΔX | Orthogonal distance regression (both uncertainties used). |
| No uncertainties (tick **infer ΔY if missing**, or run with `-i`) | Ordinary least squares; the uncertainty is estimated from the scatter. χ² and p-value are not available, and parameter errors assume the model is correct. |

**Reading χ²/ν:**
- **≈ 1**: the model fits within your uncertainties.
- **≫ 1**: the model doesn't describe the data, or the uncertainties are too small.
- **≪ 1**: the uncertainties may be overestimated, or the model has too many parameters.

LabFit prints a warning in the report when it's well outside 1. In the residual plot, most points should lie within ±1σ, almost none beyond ±3σ, and there should be no visible trend.

The report also warns if two parameters are almost perfectly correlated, if the optimiser stopped early, or if parameter uncertainties couldn't be determined.

## Keyboard shortcuts

Table shortcuts work while the table has focus.

| Shortcut | Action |
| :--- | :--- |
| `Ctrl+V` | Paste |
| `Ctrl+C` / `Ctrl+X` | Copy / cut selected cells (copies values; use right-click → *Copy as formulas* to keep formulas) |
| `Del` | Clear selected cells |
| `Ctrl+D` | Fill down (formulas shift like Excel) |
| `Enter` | Confirm the cell and move down |
| `Ctrl+Z` / `Ctrl+Y` | Undo / redo (`Ctrl+Shift+Z` also redoes) |
| `Ctrl+Shift+P` | Plot data |
| `Ctrl+Enter` | Run fit |
| `Ctrl+.` | Cancel fit |
| `Ctrl+N` | New (clears the table, asks first if there are unsaved changes) |
| `Ctrl+O` | Open CSV |
| `Ctrl+Shift+S` | Save CSV |
| `Ctrl+S` | Save plot |
| `Ctrl+P` | Print plot |
| `Ctrl+Q` | Quit |

The plot toolbar (zoom, pan, save) sits above the plot. Legend and fit-result boxes can be dragged to a better position before you save.

## Command-line options

```bash
python labfit.py [-x] [-i]
```

| Flag | Effect |
| :--- | :--- |
| `-x` | Start with a ΔX column assigned (columns are X, Y, ΔX, ΔY = A, B, C, D). |
| `-i` | Tick *infer ΔY if missing* at start-up. |

You can also pass data in from another script or a Python console:

```python
import numpy as np
import labfit

x = np.linspace(-10, 10, 30)
y = 0.1 * x + 0.5 * np.random.rand(30)
labfit.main(x=x, y=y, xerr=None, yerr=np.ones(30) * 0.05)
```

## Troubleshooting

### Windows
- **"Windows protected your PC" (SmartScreen).** The app isn't code-signed. Click **More info → Run anyway**.
- **Slow to open.** Use `LabFit-Windows-folder.zip` (see above). The first launch of the folder version can be slower while Windows scans the files; later launches are fast.
- **Make sure you extracted the zip.** Running from inside the zip viewer is slow and can fail.

### macOS
- The build is for **Apple Silicon (M1 or later)** only. Rosetta is not needed, and Intel Macs can't run it.
- **"Apple could not verify…" or the app won't open.** The app isn't notarised. Either go to *System Settings → Privacy & Security* and click **Open Anyway** after the first blocked attempt, or remove the quarantine flag in Terminal (this is the most reliable method for a folder containing many files):
  ```bash
  xattr -dr com.apple.quarantine /path/to/LabFit
  ```

### Linux
- If it won't start and mentions the Qt **"xcb"** platform plugin, install the usual X11 helper libraries (Debian/Ubuntu):
  ```bash
  sudo apt install libxcb-xinerama0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxkbcommon-x11-0
  ```
- "Permission denied": mark `LabFit/LabFit` as executable (see the table above).

### Fitting and data
- **"Fit failed / did not converge".** Try a different model, or type start values (use *Plot model* to check them first). Oscillating models (cosine, |cosine|) and peaks (Gaussian, Lorentzian) need a sensible frequency or width and position.
- **"ΔY is missing for row…".** Fill in the uncertainties, or tick *infer ΔY if missing*.
- **"ΔY must be greater than zero".** Replace zero uncertainties with a realistic value.
- **Nothing plots / wrong data plotted.** Check the X/Y/ΔX/ΔY role boxes; the column headers show the current roles. Check the status line under the table for ignored rows.
- **Log axis does nothing.** Log scales are skipped when any value is ≤ 0.
- **A cell shows `#ERR`.** Hover over it for the reason, for example an empty referenced cell, division by zero, or text where a number is expected.

## Building from source

Requires Python 3.10–3.13.

```bash
pip install numpy "scipy<1.15" matplotlib PyQt5
python labfit.py
```

`scipy` is pinned below 1.15 to avoid a crash on Apple Silicon Macs. On macOS, `pip install pyobjc-framework-Cocoa` is also needed for the Dock icon fix (optional).

### Making your own executable

```bash
pip install pyinstaller

# Folder build (fast start-up). Use ";" instead of ":" in --add-data on Windows.
pyinstaller --noconfirm --onedir --windowed --icon=app.ico --add-data "app.ico:." --name LabFit labfit.py

# Single-file build (slower start-up: unpacks on every launch)
pyinstaller --noconfirm --onefile --windowed --icon=app.ico --add-data "app.ico:." --name LabFit labfit.py
```

The `--add-data` option bundles `app.ico` so the window icon loads at runtime; `--icon` alone only sets the file's icon.

### Automatic builds

`.github/workflows/build.yml` builds the Windows (single exe and folder), macOS and Linux versions on every push and on manual runs. Download the results from the **Actions** tab (each run's *Artifacts* section) and attach the zip files to a Release.

## Credits

Original program by Wolfgang Theis (2019). Rewritten for the Y1 lab with a spreadsheet, formula engine, background fitting and diagnostics.
