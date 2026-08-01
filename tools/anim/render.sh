#!/bin/sh
# Рендер сцен в out/: mp4 1080p60 для слайдов + gif 1000px 20fps.
#
# Без аргументов гонит все шесть. Можно назвать нужные:
#   ./render.sh system_map:SystemMap
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

all="write_path:WritePath read_path:ReadPath feedback_path:FeedbackPath
     idea_synthesis:IdeaSynthesis agent_lake:AgentLake system_map:SystemMap
     system_map_wide:SystemMapWide
     write_path_v:WritePathV read_path_v:ReadPathV feedback_path_v:FeedbackPathV
     idea_synthesis_v:IdeaSynthesisV agent_lake_v:AgentLakeV"
[ $# -gt 0 ] && all="$*"

for pair in $all; do
    file=${pair%%:*}
    scene=${pair##*:}
    .venv/bin/manim -qh --disable_caching "$file.py" "$scene"

    # Не 1080p60: широкая полоса рисуется в своём разрешении, папка другая.
    mp4=$(ls -t media/videos/"$file"/*/*.mp4 | head -1)

    # Ширина гифки: полоса 850, вертикальные 800, широкие 1000.
    case "$file" in
        system_map_wide) gifw=850 ;;
        *_v) gifw=800 ;;
        *) gifw=1000 ;;
    esac
    cp "$mp4" "out/$file.mp4"

    # 20 fps. Задержка кадра в GIF хранится в сотых долях секунды и делится
    # нацело только на 50/25/20/10. Выше 20 не идём: 2 cs (50 fps) лежит на
    # пороге, ниже которого декодеры подменяют задержку своей, и ролик идёт
    # быстрее mp4; 5 cs выше любого известного порога и играется как записано.
    # 60 fps формат не берёт вовсе — половина кадров получила бы 1 cs.
    #
    # stats_mode=full + diff_mode=none: каждый кадр самодостаточный.
    # С stats_mode=diff кадры получаются частичными, и Keynote, PowerPoint и
    # Quick Look показывают такой GIF одной последней картинкой.
    ffmpeg -y -loglevel error -i "$mp4" \
        -vf "fps=20,scale=$gifw:-1:flags=lanczos,palettegen=stats_mode=full" \
        -frames:v 1 out/.palette.png
    ffmpeg -y -loglevel error -i "$mp4" -i out/.palette.png \
        -lavfi "fps=20,scale=$gifw:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3:diff_mode=none" \
        -loop 0 "out/$file.gif"
    rm -f out/.palette.png
done

ls -lh out/
