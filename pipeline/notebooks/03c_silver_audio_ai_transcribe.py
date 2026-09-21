# Databricks notebook source
# MAGIC %md
# MAGIC # 03c (alt) · Silver — audio transcription (`ai_transcribe`)
# MAGIC
# MAGIC Drop-in alternative to `03c_silver_audio.py` (which uses `ai_query` + a Whisper endpoint).
# MAGIC This version uses the built-in [`ai_transcribe`](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_transcribe)
# MAGIC AI function — **no serving endpoint to provision**. It takes the `binaryFile` `content` column
# MAGIC directly and runs as a batch SQL stage. Writes the **same table + schema** as the Whisper version,
# MAGIC so nb 04 UNIONs it in unchanged (run *either* 03c, not both).
# MAGIC
# MAGIC **Why switch:**
# MAGIC - **Built-in** — no endpoint, no base64/driver loop.
# MAGIC - **Speaker diarization** is always on — segments carry `speaker_id` ("1", "2", …), so call
# MAGIC   transcripts land as readable speaker turns (useful for adjuster review).
# MAGIC - **m4a/AAC works** — unlike the Whisper endpoint, so the ffmpeg transcode workaround is no longer
# MAGIC   needed. Supported formats: **MP3, WAV, FLAC, Opus, M4A**; ≤ **512 MB** and ≤ **1 hour** per file.
# MAGIC
# MAGIC **Return shape** (`VARIANT`): `{ error_message, response: { duration_seconds, segments: [ {start, end, speaker_id, text} ] } }`.
# MAGIC
# MAGIC **Prereqs:** Beta — a workspace admin must enable `ai_transcribe` under **Previews**; serverless
# MAGIC environment **v3+**; available in select regions. Languages tuned for **English & Spanish**.

# COMMAND ----------
# MAGIC %run ./00_config

# COMMAND ----------
# ai_transcribe(content) returns a VARIANT; error_message is per-row (a failed file doesn't fail the batch).
# We flatten the diarized segments into one readable transcript ("Speaker N: ...") for text_content, and
# keep duration_seconds / segment_count as extra columns (nb 04 only reads the 10 shared columns).
run_sql(f"""
CREATE OR REPLACE TABLE {CFG['t']['audio']} AS
WITH src AS (
  SELECT b.doc_id, b.claim_id, b.corpus, b.source_uri, b.filename, b.ext, b.modality, b.ingested_at,
         ai_transcribe(r.content) AS t
  FROM read_files('{CFG['landing_path']}', format => 'binaryFile', recursiveFileLookup => true) r
  JOIN {CFG['t']['bronze']} b ON r.path = b.source_uri
  WHERE b.route = 'audio'
)
SELECT
  doc_id, claim_id, corpus, source_uri, filename, ext, modality, ingested_at,
  t:error_message::STRING                          AS parse_error,
  t:response.duration_seconds::DOUBLE              AS duration_seconds,
  cardinality(cast(t:response.segments AS ARRAY<VARIANT>)) AS segment_count,
  array_join(
    transform(
      cast(t:response.segments AS ARRAY<VARIANT>),
      seg -> concat('Speaker ', seg:speaker_id::STRING, ': ', seg:text::STRING)
    ), '\\n'
  )                                                AS text_content
FROM src
""")

display(run_sql(f"""
SELECT filename, ext, round(duration_seconds,1) AS secs, segment_count,
       length(text_content) AS chars, parse_error, substr(text_content,1,160) AS preview
FROM {CFG['t']['audio']} ORDER BY ext, filename"""))

# COMMAND ----------
# MAGIC %md
# MAGIC ### Optional — per-segment (speaker-turn) view
# MAGIC Explodes each transcript into one row per diarized segment, with speaker + timing. Handy for QA or a
# MAGIC timeline UI; not required by the pipeline (nb 04 uses the flattened `text_content` above).

# COMMAND ----------
display(run_sql(f"""
WITH t AS (
  SELECT doc_id, filename, ai_transcribe(r.content) AS r
  FROM read_files('{CFG['landing_path']}', format => 'binaryFile', recursiveFileLookup => true) r
  JOIN {CFG['t']['bronze']} b ON r.path = b.source_uri
  WHERE b.route = 'audio'
)
SELECT t.filename,
       seg.value:speaker_id::STRING AS speaker_id,
       round(seg.value:start::DOUBLE,2) AS start_s,
       round(seg.value:`end`::DOUBLE,2) AS end_s,
       seg.value:text::STRING AS text
FROM t, LATERAL variant_explode(t.r:response.segments) AS seg
ORDER BY t.filename, start_s
"""))
