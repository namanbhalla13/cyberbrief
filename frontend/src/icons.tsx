/** One family of outline icons (24px grid, 1.7 stroke), as in the journey design. Decorative by default: a control that has only an
 * icon must carry its own accessible name (an aria-label on the button), never rely on the icon. */
const PATHS = {
  shield: "M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6l-8-3Zm-4 9 3 3 5-6",
  home: "m3 10 9-7 9 7M5 9v12h14V9M9 21v-8h6v8",
  plus: "M12 5v14M5 12h14",
  folder: "M3 7h7l2-3h8v16H3V7Z",
  help: "M9 9a3 3 0 1 1 5 2c-2 1-2 2-2 3M12 17h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
  chat: "M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z",
  arrow: "M5 12h14m-5-5 5 5-5 5",
  back: "M19 12H5m5-5-5 5 5 5",
  check: "m5 12 4 4L19 6",
  file: "M5 3h9l5 5v13H5V3Zm9 0v6h5M8 13h8M8 17h6",
  clip: "m8 12 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l8-8",
  edit: "m14 4 6 6M4 20l5-1L21 7l-5-5L4 14v6Z",
  eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Zm13 0a3 3 0 1 1-6 0 3 3 0 0 1 6 0",
  close: "m6 6 12 12M6 18 18 6",
  search: "M10 17a7 7 0 1 0 0-14 7 7 0 0 0 0 14Zm5-2 6 6",
  spark: "m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3 3-7Z",
  users: "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM2 21v-3a6 6 0 0 1 12 0v3M17 4a4 4 0 0 1 0 8m1 4a5 5 0 0 1 4 5",
  mail: "M3 5h18v14H3V5Zm0 1 9 7 9-7",
  bank: "m3 7 9-5 9 5H3Zm2 3v8m7-8v8m7-8v8M2 21h20",
  list: "M9 6h12M9 12h12M9 18h12M3 6h1M3 12h1M3 18h1",
  refresh: "M20 10a8 8 0 1 0 0 5M20 3v7h-7",
  download: "M12 3v13m-4-4 4 4 4-4M4 17v4h16v-4",
  link: "m9 15 6-6M8 16l-2 2a4 4 0 0 1-5-5l5-5M16 8l2-2a4 4 0 0 0-5-5l-5 5",
  clock: "M12 7v5l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
  alert: "M12 4 2 20h20L12 4Zm0 6v5m0 3h.01",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d={PATHS[name]}/></svg>;
}

/** A square icon-only button: always has a name, a 44px target and a visible focus ring. */
export function IconButton({ name, label, onClick, disabled, type = "button" }: { name: IconName; label: string; onClick?: () => void; disabled?: boolean; type?: "button" | "submit" }) {
  return <button type={type} className="secondary icon-button" aria-label={label} title={label} onClick={onClick} disabled={disabled}><Icon name={name} size={18}/></button>;
}

/** The page head of the journey design: a small label, the title, one line of purpose, and at most one primary action. */
export function PageHead({ eyebrow, title, lead, action }: { eyebrow?: string; title: string; lead?: string; action?: React.ReactNode }) {
  return <div className="pagehead"><div>{eyebrow && <div className="eyebrow">{eyebrow}</div>}<h1>{title}</h1>{lead && <p>{lead}</p>}</div>{action}</div>;
}

/** The assistant's card beside the work: what it checked, who decides, and the one thing it is focused on. */
export function AgentCard({ title, subtitle, steps, focus }: { title: string; subtitle: string; steps: { title: string; text: string }[]; focus?: string }) {
  return <section className="card agentcard" aria-label={title}>
    <div className="agenthead"><span className="tileicon"><Icon name="spark"/></span><div><h3>{title}</h3><p>{subtitle}</p></div></div>
    {steps.map((s) => <div className="agentstep" key={s.title}><strong>{s.title}</strong><span>{s.text}</span></div>)}
    {focus && <div className="agentdecision"><b className="small">{focus.split("|")[0]}</b><p>{focus.split("|")[1]}</p></div>}
  </section>;
}
