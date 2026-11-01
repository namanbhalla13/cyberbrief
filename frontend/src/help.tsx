/** Help: how a report moves, what the assistant can do, and the official way to report, which this demo never replaces. */
import { Link } from "react-router-dom";
import { useI18n } from "./ui";

export function HelpPage() {
  const { t } = useI18n();
  return <>
    <h1> {t("Help")} </h1>
    <section className="card" aria-label={t("Urgent? Report it officially first")}>
      <h2> {t("Urgent? Report it officially first")} </h2>
      <p> {t("Preparing a report here never has to delay an official one. Call 1930 or use the official portal, then come back to organise your evidence.")} </p>
      <p className="inline-form"><a className="button" href="tel:1930"> {t("Call 1930")} </a>
        <a className="button secondary" href="https://cybercrime.gov.in/" target="_blank" rel="noreferrer noopener"> {t("Official reporting portal ↗")} </a></p>
    </section>
    <section className="card" aria-label={t("How a report moves")}>
      <h2> {t("How a report moves")} </h2>
      <ol>
        <li><strong>{t("Describe the problem")}</strong> — {t("Talk or type in Hindi, English or both, or attach a screenshot.")}</li>
        <li><strong>{t("Check the important details")}</strong> — {t("Confirm each payment, or correct it.")}</li>
        <li><strong>{t("Answer only what is needed")}</strong> — {t("Missing details and the area, so it can reach the right reviewer.")}</li>
        <li><strong>{t("Send it for review")}</strong> — {t("A coordinator gives it to an officer who covers your area.")}</li>
        <li><strong>{t("Track it")}</strong> — {t("Your report page says where it stands, who acts next, and what you can do.")}</li>
        <li><strong>{t("Answer a specific request")}</strong> — {t("If your reviewer needs one thing, you answer just that.")}</li>
      </ol>
    </section>
    <section className="card" aria-label={t("About this demo")}>
      <h2> {t("About this demo")} </h2>
      <ul className="plain-list">
        <li>{t("All people, banks and reports here are fictional. The officer, the coordinator and the bank are simulated.")}</li>
        <li>{t("Nothing here files an official complaint, contacts a real bank, or changes an official status.")}</li>
        <li>{t("Never type an OTP, UPI PIN, password or card number into this app.")}</li>
      </ul>
      <p className="inline-form"><Link className="button" to="/assistant"> {t("Start a report")} </Link>
        <Link className="button secondary" to="/register"> {t("Fill in a form instead")} </Link></p>
    </section>
  </>;
}
