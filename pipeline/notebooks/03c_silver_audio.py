# Databricks notebook source
# MAGIC %md
# MAGIC # 03c · Silver — audio transcription (Whisper via `ai_query`)
# MAGIC
# MAGIC Transcribes `route='audio'` files with **`ai_query`**, passing the `binaryFile` `content` column
# MAGIC directly to the `whisper-large-v3` endpoint — no base64/driver loop, and it scales as a batch SQL
# MAGIC stage. Pattern per
# MAGIC [Streamline Call Center Transcript Analytics](https://community.databricks.com/t5/technical-blog/streamline-customer-call-center-transcripts-analytics-with/ba-p/101689).
# MAGIC
# MAGIC Output lands in the same Silver schema as the other branches, so nb 04 UNIONs it in unchanged.
# MAGIC
# MAGIC **Validated against this endpoint:** mp3 works, incl. multi-minute files (5.5-min call ≈ 44s, the
# MAGIC endpoint chunks server-side). Size: `ai_query` input ≤ 16MB, Whisper ≤ 25MB — our audio fits.
# MAGIC
# MAGIC **m4a/AAC caveat:** this endpoint cannot decode AAC, so m4a rows return a per-row error (kept in
# MAGIC `parse_error` via `failOnError => false`, not a batch failure). To include m4a, normalize it to
# MAGIC wav/mp3 at ingest — see the optional cell at the bottom.

# COMMAND ----------
# MAGIC %run ./00_config

# COMMAND ----------
# ai_query passes the audio bytes straight to Whisper; failOnError => false yields {result, errorMessage}.
run_sql(f"""
CREATE OR REPLACE TABLE {CFG['t']['audio']} AS
WITH src AS (
  SELECT b.doc_id, b.claim_id, b.corpus, b.source_uri, b.filename, b.ext, b.modality, b.ingested_at,
         ai_query('{CFG['audio_endpoint']}', r.content, failOnError => false) AS q
  FROM read_files('{CFG['landing_path']}', format => 'binaryFile', recursiveFileLookup => true) r
  JOIN {CFG['t']['bronze']} b ON r.path = b.source_uri
  WHERE b.route = 'audio'
)
SELECT doc_id, claim_id, corpus, source_uri, filename, ext, modality, ingested_at,
       q.errorMessage AS parse_error,
       q.result       AS text_content
FROM src
""")

display(run_sql(f"""
SELECT filename, ext, length(text_content) AS chars, parse_error, substr(text_content,1,120) AS preview
FROM {CFG['t']['audio']} ORDER BY ext, filename"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ### Optional — normalize m4a→wav so it transcribes too
# MAGIC Run only if you have m4a files (this endpoint can't decode AAC). Transcodes each m4a in the landing
# MAGIC volume to a sibling `.wav`, which the Bronze `audio` route then picks up on the next run. Requires
# MAGIC `ffmpeg` on the cluster (ML runtimes include it). After running, re-run nb 01 then this notebook.

# COMMAND ----------
# import subprocess, shutil, os
# assert shutil.which("ffmpeg"), "ffmpeg not on cluster"
# for r in spark.sql(f"SELECT source_uri FROM {CFG['t']['bronze']} WHERE route='audio' AND ext='m4a'").collect():
#     src = r["source_uri"].replace("dbfs:", "")           # /Volumes/... (FUSE-mounted)
#     dst = os.path.splitext(src)[0] + ".wav"
#     subprocess.run(["ffmpeg", "-y", "-i", src, "-ac", "1", "-ar", "16000", dst], check=True)
#     print("wrote", dst)
