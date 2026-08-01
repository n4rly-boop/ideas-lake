#!/bin/sh
# Три PNG словаря фигур в out/: 512×512, прозрачный фон (-t).
set -e
cd "$(dirname "$0")"
mkdir -p out

for pair in Idea:idea Thesis:thesis RunLog:runlog; do
    scene=${pair%%:*}
    name=${pair##*:}
    .venv/bin/manim -s -t --disable_caching shapes.py "$scene"
    cp media/images/shapes/"$scene"_ManimCE_v*.png "out/shape-$name.png"
done

ls -lh out/shape-*.png
