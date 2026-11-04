#!/bin/bash
# Run inside the unmodified ubuntu:24.04 image: no apt, Python, X11, or host libs.
# Arguments: AppImage, read-only fixture directory, writable evidence directory.
set -euo pipefail
image=$1
fixtures=$2
output=$3
mkdir -p "$output/home" "$output/ملفات عربية"
output=$(realpath "$output")
exec > >(tee "$output/commands.log") 2>&1
set -x
export HOME="$output/home" XDG_CONFIG_HOME="$output/home/.config"
export XDG_CACHE_HOME="$output/home/.cache" XDG_DATA_HOME="$output/home/.local/share"
export QT_QPA_PLATFORM=offscreen APPIMAGE_EXTRACT_AND_RUN=1
unset PYTHONPATH PYTHONHOME LD_LIBRARY_PATH DYLD_LIBRARY_PATH GS_LIB TESSDATA_PREFIX QT_PLUGIN_PATH QT_QPA_PLATFORM_PLUGIN_PATH QML2_IMPORT_PATH
cp "$fixtures/"*.pdf "$output/ملفات عربية/"
folder="$output/ملفات عربية"
source="$folder/شهادة 2026.pdf"
(cd "$folder"; sha256sum -- *.pdf) > "$output/original-sha256.txt"
"$image" --packaging-smoke-output "$output" "$folder" "$source"
test -s "$output/powerpdf-gui.png"
test -s "$output/runtime.json"
grep -q '"window_class": "MainWindow"' "$output/runtime.json"
grep -q '"preview_pages": 2' "$output/runtime.json"
"$image" --version
"$image" --runtime-info > "$output/console-runtime.json"
grep -q '/vendor/ghostscript/' "$output/console-runtime.json"
grep -q '/vendor/tesseract/' "$output/console-runtime.json"
"$image" --cli compress "$source" -q ebook
"$image" --cli image "$source" -f png -r 150
"$image" --cli split "$source"
pages="$folder/شهادة 2026_pages"
test "$(find "$pages" -maxdepth 1 -name '*.pdf' | wc -l)" -eq 2
"$image" --cli merge "$pages/شهادة 2026_page_001.pdf" "$pages/شهادة 2026_page_002.pdf" -o "$folder/مدمج.pdf"
for document in "$folder/شهادة 2026_small.pdf" "$folder/مدمج.pdf"; do
    test -s "$document"
    "$image" --cli image "$document" -f png -r 100
done
test "$(stat -c %s "$folder/شهادة 2026_small.pdf")" -lt "$(stat -c %s "$source")"
for stem in 'شهادة 2026' 'شهادة 2026_small' 'مدمج'; do
    test "$(find "$folder/${stem}_images" -maxdepth 1 -name '*.png' | wc -l)" -eq 2
done
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
(cd "$work"; "$image" --appimage-extract > "$output/appimage-extract.log")
payload="$work/squashfs-root/usr/lib/powerpdf/_internal"
export LD_LIBRARY_PATH="$payload/vendor/lib:$payload/vendor/ghostscript/lib:$payload/vendor/tesseract/lib"
export GS_LIB="$payload/vendor/ghostscript/lib:$payload/vendor/ghostscript/Resource/Init:$payload/vendor/ghostscript/Resource/Font"
export TESSDATA_PREFIX="$payload/vendor/tesseract/tessdata"
gs="$payload/vendor/ghostscript/bin/gs"
tess="$payload/vendor/tesseract/bin/tesseract"
"$gs" --version
"$tess" --version
"$tess" --list-langs > "$output/ocr-languages.txt"
for language in eng ara osd; do grep -qx "$language" "$output/ocr-languages.txt"; done
"$gs" -q -dSAFER -dBATCH -dNOPAUSE -dFirstPage=1 -dLastPage=1 -sDEVICE=png16m -r180 "-sOutputFile=$output/ocr-page.png" "$source"
for language in eng ara osd; do
    mode=6
    if test "$language" = osd; then mode=0; fi
    "$tess" "$output/ocr-page.png" stdout -l "$language" --psm "$mode" > "$output/ocr-$language.txt" 2> "$output/ocr-$language.stderr.txt"
    if grep -qi 'function not present' "$output/ocr-$language.stderr.txt"; then exit 1; fi
done
grep -q PowerPDF "$output/ocr-eng.txt"
grep -q 'Orientation in degrees:' "$output/ocr-osd.txt"
# Record linkage from the extracted artifact, not the build/runner vendor tree.
{
    for binary in "$gs" "$tess"; do printf '\n%s\n' "$binary"; ldd "$binary"; done
    export LD_LIBRARY_PATH="$payload:$payload/PySide6/Qt/lib:$LD_LIBRARY_PATH"
    find "$payload" \( -name '*Qt6PrintSupport*.so*' -o -name '*qoffscreen*.so' -o -name '*libpdfium*.so' \) -type f -print -exec ldd '{}' \;
} > "$output/native-linkage.txt" 2>&1
cat "$output/native-linkage.txt"
! grep -q 'not found' "$output/native-linkage.txt"
grep -q Qt6PrintSupport "$output/native-linkage.txt"
grep -q qoffscreen "$output/native-linkage.txt"
grep -q libpdfium "$output/native-linkage.txt"
(cd "$folder"; sha256sum --check "$output/original-sha256.txt")
printf '{"status":"passed","base":"ubuntu:24.04","apt_installs":false}\n' > "$output/product-smoke.json"
