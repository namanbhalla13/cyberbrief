/** Step two: uploading screenshots, typing an account and recording one. */
import { useCallback, useEffect, useMemo, useRef, useState, type ChangeEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, TERMINAL_RUN_STATES, api, newIdempotencyKey, type RunOut, type SourceOut } from "./api";
import { AiModeBadge, ErrorNotice, JourneySteps, caseReference, useCase, useI18n } from "./ui";


// ---- from features/intake/recorder/encodeWav16 ------------------------------------------------

/**
 * Convert any browser recording (webm/opus, mp4/aac, ogg…) to 16 kHz mono 16-bit PCM WAV
 * entirely in the browser, so the server never needs FFmpeg. Mixdown and resampling are done
 * by an OfflineAudioContext.
 */
export async function blobToWav16k(blob: Blob): Promise<{ wav: Blob; durationMs: number }> {
  const bytes = await blob.arrayBuffer();
  const decodeContext = new AudioContext();
  let decoded: AudioBuffer;
  try {
    decoded = await decodeContext.decodeAudioData(bytes.slice(0));
  } finally {
    await decodeContext.close();
  }
  const targetRate = 16000;
  const length = Math.max(1, Math.ceil(decoded.duration * targetRate));
  const offline = new OfflineAudioContext(1, length, targetRate);
  const source = offline.createBufferSource();
  source.buffer = decoded;
  source.connect(offline.destination);
  source.start(0);
  const rendered = await offline.startRendering();
  const samples = rendered.getChannelData(0);
  return { wav: encodePcm16Wav(samples, targetRate), durationMs: Math.round((samples.length / targetRate) * 1000) };
}

export function encodePcm16Wav(samples: Float32Array, sampleRate: number): Blob {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const writeString = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  };
  writeString(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true); // PCM chunk size
  view.setUint16(20, 1, true); // PCM format
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  writeString(36, "data");
  view.setUint32(40, samples.length * 2, true);
  let offset = 44;
  for (let i = 0; i < samples.length; i++) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    offset += 2;
  }
  return new Blob([buffer], { type: "audio/wav" });
}


// ---- from features/intake/recorder/useRecorder ------------------------------------------------

export type RecorderState = "idle" | "recording" | "stopped" | "unsupported" | "denied";

/** Microphone recording with a hard time cap. Produces a Blob in whatever format the browser prefers. */
export function useRecorder(maxSeconds = 120) {
  const [state, setState] = useState<RecorderState>(() =>
    typeof MediaRecorder === "undefined" || !navigator.mediaDevices?.getUserMedia ? "unsupported" : "idle",
  );
  const [seconds, setSeconds] = useState(0);
  const [blob, setBlob] = useState<Blob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const chunks = useRef<BlobPart[]>([]);
  const timer = useRef<number | null>(null);

  const cleanup = useCallback(() => {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = null;
    stream.current?.getTracks().forEach((t) => t.stop());
    stream.current = null;
  }, []);

  useEffect(() => cleanup, [cleanup]);

  const stop = useCallback(() => {
    if (recorder.current && recorder.current.state !== "inactive") recorder.current.stop();
  }, []);

  const start = useCallback(async () => {
    setError(null);
    setBlob(null);
    chunks.current = [];
    try {
      stream.current = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setState("denied");
      setError("Microphone access was not granted. You can type your account instead.");
      return;
    }
    const mimeType = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg"].find((t) => MediaRecorder.isTypeSupported(t));
    const mediaRecorder = new MediaRecorder(stream.current, mimeType ? { mimeType } : undefined);
    recorder.current = mediaRecorder;
    mediaRecorder.ondataavailable = (event) => {
      if (event.data.size > 0) chunks.current.push(event.data);
    };
    mediaRecorder.onstop = () => {
      setBlob(new Blob(chunks.current, { type: mediaRecorder.mimeType || "audio/webm" }));
      setState("stopped");
      cleanup();
    };
    mediaRecorder.start(250);
    setSeconds(0);
    setState("recording");
    const startedAt = Date.now();
    timer.current = window.setInterval(() => {
      const elapsed = Math.floor((Date.now() - startedAt) / 1000);
      setSeconds(elapsed);
      if (elapsed >= maxSeconds) stop();
    }, 250);
  }, [cleanup, maxSeconds, stop]);

  const reset = useCallback(() => {
    setBlob(null);
    setSeconds(0);
    setState(typeof MediaRecorder === "undefined" ? "unsupported" : "idle");
  }, []);

  return { state, seconds, blob, error, start, stop, reset };
}


// ---- from features/intake/recorder/RecordAccount ----------------------------------------------

/** Record a spoken account (Hindi, English or Hinglish), convert it to 16 kHz WAV in the browser, and hand it over. */
export function RecordAccount({ onWav, disabled }: {
    onWav: (wav: Blob, filename: string) => Promise<void>;
    disabled?: boolean;
}) {
    const { t } = useI18n();
    const recorder = useRecorder(120);
    const [busy, setBusy] = useState(false);
    const [message, setMessage] = useState<string | null>(null);
    const previewUrl = useMemo(() => (recorder.blob ? URL.createObjectURL(recorder.blob) : null), [recorder.blob]);
    useEffect(() => () => { if (previewUrl)
        URL.revokeObjectURL(previewUrl); }, [previewUrl]);
    async function useRecording() {
        if (!recorder.blob)
            return;
        setBusy(true);
        setMessage(null);
        try {
            const { wav, durationMs } = await blobToWav16k(recorder.blob);
            await onWav(wav, `recording-${Math.round(durationMs / 1000)}s.wav`);
            setMessage("Recording saved. You can continue to check the details.");
            recorder.reset();
        }
        catch (e) {
            setMessage(e instanceof Error ? e.message : "The recording could not be converted or uploaded. You can type your account instead.");
        }
        finally {
            setBusy(false);
        }
    }
    async function onWavFile(event: ChangeEvent<HTMLInputElement>) {
        const file = event.target.files?.[0];
        event.target.value = "";
        if (!file)
            return;
        setBusy(true);
        setMessage(null);
        try {
            await onWav(file, file.name);
            setMessage("File saved");
        }
        catch (e) {
            setMessage(e instanceof Error ? e.message : "Upload failed.");
        }
        finally {
            setBusy(false);
        }
    }
    return (<div className="recorder">
      <h2> {t("Record your account")} </h2>
      <p className="small muted"> {t("Speak naturally in Hindi, English or both. Up to 2 minutes each, and 3 recordings per report. Listen once before saving.")} </p>
      {recorder.state === "unsupported" && <p className="warn small"> {t("This browser cannot record audio. Upload a WAV file below or type your account.")} </p>}
      {recorder.error && <p className="error small"> {t(recorder.error)} </p>}
      <div className="actions">
        {recorder.state === "recording" ? (<button type="button" onClick={recorder.stop} disabled={disabled}> {t("Stop (")} {recorder.seconds} {t("s)")} </button>) : (<button type="button" onClick={() => void recorder.start()} disabled={disabled || busy || recorder.state === "unsupported"}> {t(recorder.state === "stopped" ? "Record again" : "Start recording")} </button>)}
        {recorder.state === "stopped" && recorder.blob && (<button type="button" onClick={() => void useRecording()} disabled={busy || disabled}> {t(busy ? "Converting…" : "Use this recording")} </button>)}
      </div>
      {previewUrl && <audio controls src={previewUrl} aria-label={t("Preview of your recording")}/>}
      <label className="small"> {t("Or upload a WAV file (16 kHz or 24 kHz, mono, 16-bit)")} <input type="file" accept="audio/wav,.wav" onChange={onWavFile} disabled={busy || disabled} aria-label={t("Choose WAV file")}/>
      </label>
      {message && <p className="small"> {t(message)} </p>}
    </div>);
}


// ---- from features/intake/AddMaterialPage -----------------------------------------------------

const KIND_LABELS: Record<string, string> = {
    image: "Receipt / screenshot",
    typed_account: "Typed account",
    user_answer: "Typed answer",
    audio: "Recorded account",
    transcript: "Transcript",
    supplied_official_update: "Supplied official update",
};
export function AddMaterialPage() {
    const { t } = useI18n();
    const { caseId } = useParams();
    const navigate = useNavigate();
    const { snapshot, error, reload, setError } = useCase(caseId);
    const [uploading, setUploading] = useState<string[]>([]);
    const [uploadKind, setUploadKind] = useState<"image" | "supplied_official_update">("image");
    const [text, setText] = useState("");
    const [polling, setPolling] = useState<string | null>(null);
    const [continuing, setContinuing] = useState(false);
    const pollTimer = useRef<number | null>(null);
    useEffect(() => () => { if (pollTimer.current)
        window.clearTimeout(pollTimer.current); }, []);
    // a case loaded from a demo pack arrives with a run already in flight: poll it
    useEffect(() => {
        if (snapshot?.case.activeRunId && polling === null) {
            const active = snapshot.runs.find((r) => r.id === snapshot.case.activeRunId);
            if (active && !TERMINAL_RUN_STATES.has(active.status))
                pollRun(active.id);
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [snapshot?.case.activeRunId]);
    if (!caseId)
        return null;
    async function onFiles(event: ChangeEvent<HTMLInputElement>) {
        const files = Array.from(event.target.files ?? []);
        event.target.value = "";
        for (const file of files) {
            setUploading((prev) => [...prev, file.name]);
            try {
                await api.uploadImage(caseId!, file, newIdempotencyKey(), undefined, uploadKind);
            }
            catch (e) {
                setError(e instanceof ApiError ? `${file.name}: ${e.message} ${e.nextStep ?? ""}` : `${file.name}: upload failed`);
            }
            finally {
                setUploading((prev) => prev.filter((n) => n !== file.name));
            }
        }
        await reload();
    }
    async function addText() {
        if (!text.trim())
            return;
        try {
            await api.addText(caseId!, text.trim(), "typed_account", newIdempotencyKey());
            setText("");
            await reload();
        }
        catch (e) {
            setError(e instanceof Error ? e.message : "Could not add the text");
        }
    }
    function pollRun(runId: string) {
        setPolling(runId);
        const tick = async () => {
            try {
                const run = await api.getRun(caseId!, runId);
                if (TERMINAL_RUN_STATES.has(run.status)) {
                    setPolling(null);
                    await reload();
                    return;
                }
            }
            catch {
                setPolling(null);
                return;
            }
            pollTimer.current = window.setTimeout(tick, 1500);
        };
        void tick();
    }
    async function startRun(retryRunId?: string) {
        if (!snapshot)
            return;
        setError(null);
        try {
            const run = await api.createRun(caseId!, retryRunId ? { retryRunId } : { expectedRevision: snapshot.case.currentRevision }, newIdempotencyKey());
            if (TERMINAL_RUN_STATES.has(run.status))
                await reload();
            else
                pollRun(run.id);
        }
        catch (e) {
            setError(e instanceof ApiError ? `${e.message} ${e.nextStep ?? ""}` : "Could not start processing");
        }
    }
    /**
     * Start the run and hand the citizen straight back to their complaint. The case page polls
     * an in-flight run on arrival, so they watch "Reading your evidence…" there rather than
     * waiting on this screen and then being asked to follow a link.
     */
    async function submitEvidence() {
        if (!snapshot)
            return;
        setContinuing(true);
        setError(null);
        try {
            if (text.trim()) {
                await api.addText(caseId!, text.trim(), "typed_account", newIdempotencyKey());
                setText("");
            }
            const current = await api.getCase(caseId!);
            if (current.sources.some((s) => ["stored", "failed"].includes(s.status))) {
                await api.createRun(caseId!, { expectedRevision: current.case.currentRevision }, newIdempotencyKey());
            }
            navigate(`/cases/${caseId}`);
        }
        catch (e) {
            setError(e instanceof ApiError ? `${e.message} ${e.nextStep ?? ""}` : "Could not start processing");
        }
        finally {
            setContinuing(false);
        }
    }
    const unprocessed = snapshot?.sources.filter((s) => ["stored", "failed"].includes(s.status) && ["image", "supplied_official_update", "audio", "transcript", "typed_account", "user_answer"].includes(s.kind)) ?? [];
    const activeRun = snapshot?.runs.find((r) => r.id === snapshot.case.activeRunId);
    const added = snapshot?.sources.length ?? 0;
    // a run is in flight either because this page started it or because the case arrived with one
    const reading = polling !== null || Boolean(activeRun && !TERMINAL_RUN_STATES.has(activeRun.status));
    return (<div className="citizen-flow">
      <JourneySteps current={2}/>
      <h1> {t("Add photos or a recording")} </h1>
      {snapshot && (<p className="case-meta muted">
          <span>{snapshot.case.title}</span>
          <span className="caseref">{caseReference(caseId)}</span>
        </p>)}
      <p className="muted"> {t("Choose what is easiest for you. A payment screenshot, a message or your own words is enough to start.")} </p>
      {error && <ErrorNotice message={error}/>}

      <section>
        <div className="card">
          <h2> {t("Screenshots and receipts")} </h2>
          <p className="small muted"> {t("Add up to 10 photos (JPG, PNG or WebP), up to 10 MB each.")} </p>
          <details className="optional-details"><summary> {t("Is this an official message?")} </summary>
          <label className="small"> {t("What are you uploading?")} <select value={uploadKind} onChange={(e) => setUploadKind(e.target.value as "image" | "supplied_official_update")} aria-label={t("Upload kind")}>
              <option value="image"> {t("A payment receipt, bank screenshot or chat screenshot")} </option>
              <option value="supplied_official_update"> {t("An official update I received (acknowledgement, message)")} </option>
            </select>
          </label>
          </details>
          <input type="file" accept="image/png,image/jpeg,image/webp" multiple onChange={onFiles} disabled={reading || continuing || uploading.length > 0} aria-label={t("Choose images")}/>
          {uploading.length > 0 && <p className="muted small"> {t("Uploading")} {uploading.join(", ")}…</p>}
          <details className="intake-option"><summary> {t("Type or paste a message")} </summary>
          <p className="small muted"> {t("Paste what the seller sent you, or describe what happened.")} </p>
          <textarea value={text} onChange={(e) => setText(e.target.value)} rows={4} maxLength={10000} placeholder={t("Say what happened in your own words (Hindi, English or Hinglish).")} aria-label={t("Typed account")}/>
          <button type="button" onClick={() => void addText()} disabled={!text.trim() || reading || continuing}> {t("Add typed account")} </button>
          </details>
          <details className="intake-option"><summary> {t("Speak in Hindi or English")} </summary>
          <RecordAccount disabled={uploading.length > 0 || reading || continuing} onWav={async (wav, filename) => {
            setUploading((prev) => [...prev, filename]);
            try {
                await api.uploadAudio(caseId!, wav, filename, newIdempotencyKey());
                await reload();
            }
            catch (e) {
                const text = e instanceof ApiError ? `${e.message} ${e.nextStep ?? ""}`.trim() : "Upload failed";
                setError(text);
                throw new Error(text);
            }
            finally {
                setUploading((prev) => prev.filter((n) => n !== filename));
            }
        }}/>
          </details>
        </div>

        <div className="card">
          <h2> {t("Ready for the next step?")} </h2>
          <p className="small" role="status"> {t(reading ? "Reading your evidence…" : unprocessed.length === 0 ? added === 0 ? "Add at least one screenshot, message or recording first." : "Everything you have added has been read." : "Your files are ready. Next, check the details we read.")} </p>
          <button type="button" onClick={() => void submitEvidence()} disabled={!snapshot || uploading.length > 0 || (added === 0 && !text.trim()) || reading || continuing}> {t(reading || continuing ? "Reading…" : uploading.length > 0 ? "Uploading…" : "Continue to check →")} </button>
          {snapshot?.case.demoPackId && snapshot.case.demoStepsRemaining > 0 && (<button type="button" className="secondary" disabled={reading} onClick={async () => {
                try {
                    const snap = await api.nextDemoUpdate(caseId!);
                    if (snap.case.activeRunId)
                        pollRun(snap.case.activeRunId);
                    else
                        await reload();
                }
                catch (e) {
                    setError(e instanceof Error ? e.message : "Could not add the demo update");
                }
            }}>{t("Demo: add the next staged update ({n} left)", { n: snapshot.case.demoStepsRemaining })}</button>)}
          {snapshot && snapshot.runs.length > 0 && (<details>
              <summary>{t("Processing details ({n} run(s))", { n: snapshot.runs.length })}</summary>
              <ul className="plain runs">
                {snapshot.runs.map((run) => <RunRow key={run.id} run={run} onRetry={() => void startRun(run.id)}/>)}
              </ul>
            </details>)}
          <p className="small">
            <Link to={`/cases/${caseId}`}> {t("\u2190 Back to my complaint")} </Link>
          </p>
        </div>
      </section>

      <section className="card">
        <h2> {t("What you have added")} </h2>
        {snapshot && snapshot.sources.length === 0 && <p className="muted"> {t("Nothing added yet.")} </p>}
        <ul className="plain sources">
          {snapshot?.sources.map((s) => (<li key={s.id} className="sourcerow">
              {s.hasThumbnail ? (<a href={api.sourceUrl(caseId, s.id)} target="_blank" rel="noreferrer noopener">
                  <img src={api.sourceUrl(caseId, s.id, "thumbnail")} alt={t("Thumbnail of {0}", {0: s.displayFilename})} className="thumb"/>
                </a>) : (<div className="thumb placeholder" aria-hidden="true"> {t(s.kind === "image" ? "image" : "text")} </div>)}
              <div>
                <strong>{s.displayFilename}</strong> <span className="badge"> {t(KIND_LABELS[s.kind] ?? s.kind)} </span>{" "}
                <span className={`badge badge-status-${s.status}`}> {t(s.status)} </span>
                <div className="muted small"> {t("Added")} {new Date(s.receivedAt).toLocaleString()}
                  {s.duplicateOfSourceId && <> {t("\u00B7 identical bytes to an earlier upload")} </>}
                  <details>
                    <summary> {t("File details")} </summary> {t("Revision")} {s.addedInRevision} · {s.byteSize.toLocaleString()} {t("bytes")} · <span className="mono">{s.sha256.slice(0, 12)}…</span>
                  </details>
                </div>
                {s.kind === "audio" && <audio controls preload="none" src={api.sourceUrl(caseId, s.id)} aria-label={t("Recording {0}", {0: s.displayFilename})}/>}
                {s.kind === "audio" && s.audioDurationMs && <span className="muted small"> {Math.round(s.audioDurationMs / 1000)} {t("s")} </span>}
                {s.kind === "transcript" && (<TranscriptBlock caseId={caseId} source={s} expectedRevision={snapshot!.case.currentRevision} onSnapshot={(snap) => { void snap; void reload(); }} onError={setError}/>)}
                {s.textContent && s.kind !== "transcript" && <blockquote className="small">{s.textContent}</blockquote>}
              </div>
            </li>))}
        </ul>
      </section>
    </div>);
}
function TranscriptBlock({ caseId, source, expectedRevision, onSnapshot, onError }: {
    caseId: string;
    source: SourceOut;
    expectedRevision: number;
    onSnapshot: (s: unknown) => void;
    onError: (m: string | null) => void;
}) {
    const { t } = useI18n();
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState(source.textContent ?? "");
    const superseded = Boolean(source.supersededBySourceId);
    async function save() {
        try {
            onSnapshot(await api.correctTranscript(caseId, source.id, draft.trim(), expectedRevision));
            setEditing(false);
            onError(null);
        }
        catch (e) {
            onError(e instanceof ApiError ? `${e.message} ${e.nextStep ?? ""}`.trim() : "Could not save the correction");
        }
    }
    return (<div className="transcript">
      <blockquote className="small">{source.textContent}</blockquote>
      <p className="small muted">{source.provenanceKind === "user_corrected" ? t("Corrected by you") : t("Transcribed in {0} mode", {0: source.transcriptionMode ?? "unknown"})}
{source.transcriptTurns && source.transcriptTurns.length > 0 && <> · {source.transcriptTurns.length} {t("turn(s) with time ranges")} </>}
        {superseded && <> {t("\u00B7 superseded by a corrected transcript")} </>}
      </p>
      {!superseded && !editing && <button type="button" className="secondary" onClick={() => setEditing(true)}> {t("Correct this transcript")} </button>}
      {editing && (<>
          <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={4} aria-label={t("Corrected transcript")}/>
          <div className="actions">
            <button type="button" onClick={() => void save()} disabled={!draft.trim() || draft.trim() === source.textContent}> {t("Save corrected transcript")} </button>
            <button type="button" className="secondary" onClick={() => setEditing(false)}> {t("Cancel")} </button>
          </div>
          <p className="small muted"> {t("The original transcript and the recording are kept. The corrected transcript becomes a new source you can process.")} </p>
        </>)}
    </div>);
}
function RunRow({ run, onRetry }: {
    run: RunOut;
    onRetry: () => void;
}) {
    const { t } = useI18n();
    const failedSources = Object.values(run.perSourceStatus).filter((s) => s.status === "failed").length;
    const canRetry = ["failed", "interrupted", "cancelled"].includes(run.status);
    return (<li className="runrow">
      <span className={`badge badge-run-${run.status}`}>{run.status}</span> <AiModeBadge mode={run.aiMode}/>
      <span className="muted small"> · {run.inputSourceIds.length} {t("source(s) \u00B7 stage")} {run.stage} {t("\u00B7 model")} {run.modelId}</span>
      {run.status === "proposed" && failedSources > 0 && <div className="warn small">{failedSources} {t("source(s) failed extraction and are not in the proposals. They remain listed as failed, not silently dropped.")} </div>}
      {run.errorMessage && <div className="error small">{run.errorType}: {run.errorMessage}</div>}
      {canRetry && <button type="button" className="linklike" onClick={onRetry}> {t("Retry")} </button>}
    </li>);
}
