#!/bin/sh
# Рендер трёх сцен в out/: mp4 1080p60 для слайдов + gif 1000px 15fps.
#
# Окружение (один раз, .venv под игнором):
#   uv venv tools/anim/.venv --python 3.12
#   VIRTUAL_ENV=tools/anim/.venv uv pip install manim
#
# Быстрый предпросмотр одной сцены: .venv/bin/manim -ql --format=gif read_path.py ReadPath
#
# GIF делается из готового mp4 через палитру — прямой gif-вывод manim
# даёт бандинг на тёмном фоне и файл втрое тяжелее.
set -e
cd "$(dirname "$0")"
mkdir -p out

for pair in write_path:WritePath read_path:ReadPath feedback_path:FeedbackPath; do
    file=${pair%%:*}
    scene=${pair##*:}
    .venv/bin/manim -qh --disable_caching "$file.py" "$scene"

    mp4=$(ls -t media/videos/"$file"/1080p60/*.mp4 | head -1)
    cp "$mp4" "out/$file.mp4"

    ffmpeg -y -loglevel error -i "$mp4" \
        -vf "fps=15,scale=1000:-1:flags=lanczos,palettegen=stats_mode=diff" \
        -frames:v 1 out/.palette.png
    ffmpeg -y -loglevel error -i "$mp4" -i out/.palette.png \
        -lavfi "fps=15,scale=1000:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3" \
        "out/$file.gif"
    rm -f out/.palette.png
done

ls -lh out/
