/** Where this report is, who is acting, and what happens next: the first thing a citizen sees on a report.
 *
 * Money, workflow and official status stay separate elsewhere on the page; this says in plain words what is being waited on, and it
 * is where a reviewer's specific request is answered, with an upload right beside it, without retelling the whole incident.
 */
import { useCallback, useEffect, useState, type ChangeEvent } from "react";
import { ApiError, api, newIdempotencyKey, type CaseSnapshot } from "./api";
import { ErrorNotice, useI18n } from "./ui";
import { wf, type Progress, type WorkflowConfig } from "./workflowapi";

export function ProgressPanel({ caseId, snapshot, onChanged }: { caseId: string; snapshot: CaseSnapshot; onChanged: () => void }) {
  const { t, language } = useI18n();
  const [progress, setProgress] = useState<Progress | null>(null);
  const [config, setConfig] = useState<WorkflowConfig | null>(null);
  // what was typed to a reviewer is kept for this browser tab, so leaving the page or a failed send loses nothing
  const [answers, setAnswersState] = useState<Record<string, string>>(() => {
    try { return JSON.parse(window.sessionStorage.getItem(`cb-answers-${caseId}`) ?? "{}"); } catch { return {}; }
  });
  const setAnswers = (next: Record<string, string>) => {
    setAnswersState(next);
    try { window.sessionStorage.setItem(`cb-answers-${caseId}`, JSON.stringify(next)); } catch { /* storage may be blocked; typing still works */ }
  };
  const [files, setFiles] = useState<Record<string, File[]>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => wf.progress(caseId).then(setProgress).catch(() => undefined), [caseId]);
  useEffect(() => { void load(); void wf.config().then(setConfig).catch(() => undefined); }, [load, snapshot.case.currentRevision, snapshot.case.reviewState]);

  if (!progress) return null;
  const clock = (iso: string) => new Intl.DateTimeFormat(language === "hi" ? "hi-IN" : "en-IN", { dateStyle: "medium", timeStyle: "short" }).format(new Date(iso));
  const canChooseArea = !progress.reviewer && snapshot.case.reviewState !== "closed";
  const open = progress.requests.filter((r) => !r.answered);
  const history = progress.requests.filter((r) => r.answered);

  async function chooseArea(area: string) {
    setError(null);
    try { await wf.setArea(caseId, area); await load(); } catch (e) { setError(e instanceof ApiError ? e.message : "Could not save the area"); }
  }
  async function send(requestId: string) {
    setBusy(true);
    setError(null);
    try {
      const sourceIds: string[] = [];
      for (const file of files[requestId] ?? []) {
        const current = await api.getCase(caseId);
        sourceIds.push((await api.uploadImage(caseId, file, newIdempotencyKey(), current.case.currentRevision)).id);
      }
      const current = await api.getCase(caseId);
      await wf.reply(caseId, requestId, { text: answers[requestId] ?? "", sourceIds, expectedRevision: current.case.currentRevision });
      setAnswers({ ...answers, [requestId]: "" });
      setFiles({ ...files, [requestId]: [] });
      await load();
      onChanged();
    } catch (e) {
      setError(e instanceof ApiError ? [e.message, e.nextStep].filter(Boolean).join(" ") : "Could not send your answer");
    } finally { setBusy(false); }
  }

  return <section className="card progress" aria-label={t("Where your report stands")}>
    <h2> {t("Where your report stands")} </h2>
    <p className="progress-step"><strong>{t(progress.step)}</strong></p>
    <dl className="kv">
      <dt> {t("Who acts next")} </dt><dd>{progress.responsible === "You" ? t("You") : progress.responsible}</dd>
      <dt> {t("What happens next")} </dt><dd>{t(progress.nextAction)}</dd>
      {progress.reviewer && <><dt> {t("Your reviewer")} </dt><dd>{progress.reviewer}</dd></>}
      {progress.areaName && <><dt> {t("Area")} </dt><dd>{progress.areaName}</dd></>}
    </dl>
    {progress.note && <p className="small">{t(progress.note)}</p>}
    {error && <ErrorNotice message={error}/>}
    {canChooseArea && config && <label> {t("Which area is this report about?")}
      <select value={progress.area ?? ""} onChange={(e) => e.target.value && void chooseArea(e.target.value)}>
        <option value="">{t("Choose an area")}</option>
        {config.areas.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
      </select>
      <span className="small muted"> {t("Used only to route your report to a reviewer who covers that area. It is never guessed.")} </span></label>}
    {open.map((r) => <div key={r.id} className="request" role="group" aria-label={t("A request from your reviewer")}>
      <p className="small muted"><time dateTime={r.createdAt}>{clock(r.createdAt)}</time> · {t("A request from your reviewer")}</p>
      <blockquote className="note-draft">{r.body}</blockquote>
      <label> {t("Your answer")}
        <textarea rows={3} maxLength={2000} value={answers[r.id] ?? ""} onChange={(e) => setAnswers({ ...answers, [r.id]: e.target.value })}/></label>
      <label className="button secondary attachbutton"> {t("Attach a clearer screenshot")}
        <input type="file" multiple accept="image/png,image/jpeg,image/webp" onChange={(e: ChangeEvent<HTMLInputElement>) => setFiles({ ...files, [r.id]: Array.from(e.target.files ?? []) })} aria-label={t("Choose files for this request")}/>
      </label>
      {(files[r.id] ?? []).length > 0 && <span className="small muted"> {(files[r.id] ?? []).map((f) => f.name).join(", ")} </span>}
      <button type="button" disabled={busy || (!(answers[r.id] ?? "").trim() && (files[r.id] ?? []).length === 0)} onClick={() => void send(r.id)}> {t(busy ? "Sending…" : "Send my answer")} </button>
    </div>)}
    {history.length > 0 && <details><summary> {t("Earlier requests")} ({history.length})</summary>
      <ul className="plain-list small">{history.map((r) => <li key={r.id}>{r.body} — {t("answered")}</li>)}</ul></details>}
  </section>;
}
