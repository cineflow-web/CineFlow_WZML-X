FROM mysterysd/wzmlx:v3

# Sinhala glyphs for hardsubbed subtitles (otherwise they render as boxes)
RUN apt-get update \
 && apt-get install -y --no-install-recommends fontconfig fonts-noto-core fonts-lklug-sinhala \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src/app

COPY requirements.txt .
RUN uv pip install --python /wzvenv/bin/python --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["bash", "start.sh"]
