/** An accessible tab list. Every panel stays mounted (hidden when not selected) so what was typed in one tab survives a visit to another.
 *
 * The selected tab lives in the address (`#evidence`), so a reload or the back button returns to it. Arrow keys, Home and End move
 * between tabs; only the selected tab is in the tab order.
 */
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

export interface TabSpec { id: string; label: string; badge?: string | null; content: ReactNode }

const fromHash = (ids: string[], fallback: string) => {
  const wanted = window.location.hash.replace("#", "");
  return ids.includes(wanted) ? wanted : fallback;
};

export function Tabs({ tabs, label, selected, onSelect }: { tabs: TabSpec[]; label: string; selected?: string; onSelect?: (id: string) => void }) {
  const ids = tabs.map((t) => t.id);
  const [own, setOwn] = useState(() => fromHash(ids, ids[0]));
  const current = selected && ids.includes(selected) ? selected : own;
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});

  const choose = useCallback((id: string, focus = false) => {
    setOwn(id);
    onSelect?.(id);
    window.history.replaceState(null, "", `#${id}`);
    if (focus) refs.current[id]?.focus();
  }, [onSelect]);

  useEffect(() => {
    const onHash = () => setOwn(fromHash(ids, ids[0]));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, [ids.join("|")]);   // eslint-disable-line react-hooks/exhaustive-deps

  function onKey(event: KeyboardEvent<HTMLDivElement>) {
    const at = ids.indexOf(current);
    const move = event.key === "ArrowRight" ? at + 1 : event.key === "ArrowLeft" ? at - 1 : event.key === "Home" ? 0 : event.key === "End" ? ids.length - 1 : null;
    if (move === null) return;
    event.preventDefault();
    choose(ids[(move + ids.length) % ids.length], true);
  }

  return <div className="tabs">
    <div role="tablist" aria-label={label} className="tablist" onKeyDown={onKey}>
      {tabs.map((tab) => <button key={tab.id} type="button" role="tab" id={`tab-${tab.id}`} aria-selected={current === tab.id} aria-controls={`panel-${tab.id}`}
        tabIndex={current === tab.id ? 0 : -1} className={`tab${current === tab.id ? " active" : ""}`} ref={(el) => { refs.current[tab.id] = el; }}
        onClick={() => choose(tab.id)}>{tab.label}{tab.badge ? <span className="tab-badge"> {tab.badge}</span> : null}</button>)}
    </div>
    {tabs.map((tab) => <div key={tab.id} role="tabpanel" id={`panel-${tab.id}`} aria-labelledby={`tab-${tab.id}`} hidden={current !== tab.id} className="tabpanel">{tab.content}</div>)}
  </div>;
}
