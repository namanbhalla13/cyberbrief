/** Routing and the page frame. */
import React from "react";
import ReactDOM from "react-dom/client";
import { Icon } from "./icons";
import { ToastProvider } from "./dialog";
import { JourneyHelp, JourneyHome, JourneyReports, ReportEvidence, ReportReview, ReportSend, ReportStory, TrackReport } from "./journey";
import { Link, NavLink, Outlet, RouterProvider, createBrowserRouter, useLocation, useNavigate, useParams, useRouteError } from "react-router-dom";
import { ConsultationPage, InboxLink, InboxPage } from "./officerconsult";
import { AdminCoordinatorPage, AdminOfficersPage, AdminPage } from "./admin";
import { AssistantPage } from "./assistant";
import { CasePage, ReceiptPage } from "./case";
import { LandingPage, LoginPage, RegisterComplaintPage } from "./entry";
import { AddMaterialPage } from "./intake";
import { OfficerCasePage, OfficerQueuePage } from "./officer";
import { ReviewPage, ReviewerPage, UpdatePage } from "./review";
import { AiModeBadge, AuthProvider, DemoBanner, I18nProvider, LanguageSwitch, RequireAuth, useAuth, useI18n } from "./ui";
import "./styles.css";


// ---- from features/layout/NotFoundPage --------------------------------------------------------

export function NotFoundPage() {
    const { t } = useI18n();
    return (<section className="card">
      <h1> {t("Page not found")} </h1>
      <p className="muted"> {t("That address does not exist in this demo.")} </p>
      <div className="actions">
        <Link className="button" to="/"> {t("Go to the start")} </Link>
      </div>
    </section>);
}
export function RouteError() {
    const { t } = useI18n();
    const error = useRouteError();
    const detail = error instanceof Error ? error.message : null;
    return (<div className="page">
      <section className="card" role="alert">
        <h1> {t("Something went wrong on this screen")} </h1>
        <p className="muted"> {t("Reload the page. If it keeps happening, go back to the start.")} </p>
        {detail && <p className="small mono">{detail}</p>}
        <div className="actions">
          <a className="button" href="/"> {t("Go to the start")} </a>
        </div>
      </section>
    </div>);
}


// ---- from features/layout/Layout --------------------------------------------------------------

const CRUMBS: [RegExp, string][] = [
  [/^\/home$/, "Your workspace"], [/^\/assistant/, "New report · Chat"], [/^\/report\//, "New report"], [/^\/reports\//, "My reports · Report"], [/^\/register/, "New report"], [/^\/track/, "My reports"],
  [/^\/cases\/[^/]+\/receipt/, "My reports · Receipt"], [/^\/cases\/[^/]+\/material/, "My reports · Add material"], [/^\/cases\/[^/]+\/review/, "My reports · Check details"],
  [/^\/cases\/[^/]+\/updates/, "My reports · Updates"], [/^\/cases\/[^/]+\/reviewer/, "My reports · Reviewer"], [/^\/cases\//, "My reports · Report"],
  [/^\/help/, "Help"], [/^\/officer\/cases\//, "Officer · Case"], [/^\/officer/, "Officer · My work"], [/^\/admin\/officers/, "Admin · Officers"], [/^\/admin\/coordinator/, "Admin · Coordinator"], [/^\/admin/, "Admin · Intake queue"],
];

function Brand({ to, className }: { to: string; className: string }) {
  return <Link to={to} className={`brand ${className}`}><span className="brandmark"><Icon name="shield"/></span>CyberBrief</Link>;
}

export function Layout() {
    const { t } = useI18n();
    const { session, status, error, logout } = useAuth();
    const { caseId } = useParams();
    const navigate = useNavigate();
    const { pathname } = useLocation();
    const signedIn = status === "authenticated" && session !== null;
    const officer = signedIn && session.accountKind === "officer";
    const admin = signedIn && session.accountKind === "admin";
    const home = signedIn ? (officer ? "/officer" : admin ? "/admin" : "/home") : "/";
    const crumb = CRUMBS.find(([re]) => re.test(pathname))?.[1];
    const who = officer ? t("Officer · {0}", {0: session.officerId ?? ""}) : admin ? t("Admin · {0}", {0: session.officerId ?? ""}) : signedIn ? t("Citizen · {0}", {0: session.mobileMasked ?? ""}) : "";
    const role = officer ? t("Assigned officer") : admin ? t("Department admin") : t("Citizen workspace");
    const initials = (officer || admin ? (session?.officerId ?? "").slice(0, 2) : (session?.mobileMasked ?? "").slice(-2)).toUpperCase() || "CB";
    async function signOut() {
        // leave the guarded route first, or RequireAuth reacts to the cleared session and bounces
        // the person to the sign-in page instead of the landing page
        navigate("/", { replace: true });
        await logout();
    }
    return (<div className={`shell${signedIn ? " with-nav" : ""}`}>
      <a href="#main-content" className="skip-link"> {t("Skip to content")} </a>
      <header className="topbar">
        <Brand to={home} className="brand-top"/>
        {signedIn && crumb && <span className="crumb">{t(crumb)}</span>}

        <div className="topbar-right">
          <LanguageSwitch />
          {session && <AiModeBadge mode={session.aiMode}/>}
          {signedIn ? (<>
              <span className="who small muted">{who}</span>
              <button type="button" className="secondary" onClick={() => void signOut()}> {t("Sign out")} </button>
            </>) : (<Link className="button secondary" to="/login"> {t("Sign in")} </Link>)}
        </div>
      </header>

      {/* The sidebar is the ink-coloured column on a desktop and a fixed bottom bar on a phone. A bottom bar rather than a drawer:
          it needs no open state and no focus trap, so every destination stays one tap away at 390px. */}
      {signedIn && (<nav className="sidenav" aria-label={t("Main")}>
          <Brand to={home} className="brand-side"/>
          <div className="navgroup">
            {officer ? (<><NavLink to="/officer" end><Icon name="folder"/> {t("Officer desk")} </NavLink><InboxLink/></>) : admin ? (<><NavLink to="/admin" end><Icon name="folder"/> {t("Admin desk")} </NavLink>
                <NavLink to="/admin/officers"><Icon name="users"/> {t("Officers")} </NavLink>
                <NavLink to="/admin/coordinator"><Icon name="spark"/> {t("Coordinator")} </NavLink></>) : (<>
                <NavLink to="/home"><Icon name="home"/> {t("Home")} </NavLink>
                <NavLink to="/report/story"><Icon name="plus"/> {t("New report")} </NavLink>
                <NavLink to="/track"><Icon name="folder"/> {t("My reports")} </NavLink>
                <NavLink to="/help"><Icon name="help"/> {t("Help")} </NavLink>
              </>)}
          </div>

          {/* One way back to the complaint. The detailed screens (material, review, updates, reviewer) still exist as routes; they are
              reached from the complaint itself rather than from a row of tabs the citizen has to understand. */}
          {caseId && (<div className="navgroup contextual">
              <h2 className="navheading"> {t("This report")} </h2>
              <NavLink to={`/cases/${caseId}`} end><Icon name="file"/> {t("Overview")} </NavLink>
            </div>)}

          <div className="navfoot">
            <div className="person"><span className="avatar-disc" aria-hidden="true">{initials}</span><div><b>{who}</b><span>{role}</span></div></div>
            <strong> {t("Lost money to a real scam?")} </strong>
            <a href="tel:1930"> {t("Call 1930")} </a>
            <a href="https://cybercrime.gov.in/" target="_blank" rel="noreferrer noopener"> {t("Official reporting portal ↗")} </a>
            <p className="fine"> {t("Fictional cases. Simulated assistance. No police or bank connection.")} </p>
          </div>
        </nav>)}

      <main className="page" id="main-content" tabIndex={-1}>
        <DemoBanner />
        {error && (<p role="alert" className="error"> {t("Session problem:")} {error}
          </p>)}
        <Outlet />
      </main>

      {signedIn && !officer && !admin && !pathname.startsWith("/assistant") && (
        <Link className="chat-fab" to="/assistant" aria-label={t("Chat with CyberBrief")}>
          <Icon name="chat"/>
          <span>{t("Chat with CyberBrief")}</span>
        </Link>)}

      <footer className="muted small"> {t("CyberBrief demo \u00B7 Fictional information only \u00B7 No connection to police or banks")} </footer>
    </div>);
}


// ---- from main --------------------------------------------------------------------------------

const router = createBrowserRouter([
  {
    path: "/",
    element: (
      <AuthProvider>
        <ToastProvider>
          <Layout />
        </ToastProvider>
      </AuthProvider>
    ),
    errorElement: <RouteError />,
    children: [
      // public
      { index: true, element: <LandingPage /> },
      { path: "login", element: <LoginPage /> },

      // signed in. The case routes keep their paths, and are not restricted to citizens so an
      // officer following a link gets the page's own notice rather than being bounced.
      {
        element: <RequireAuth />,
        children: [
          { path: "home", element: <JourneyHome /> },
          { path: "report/story", element: <ReportStory /> },
          { path: "report/evidence", element: <ReportEvidence /> },
          { path: "report/review", element: <ReportReview /> },
          { path: "report/send", element: <ReportSend /> },
          { path: "reports/:caseId", element: <TrackReport /> },
          { path: "register", element: <RegisterComplaintPage /> },
          { path: "track", element: <JourneyReports /> },
          { path: "assistant", element: <AssistantPage /> },
          { path: "help", element: <JourneyHelp /> },
          { path: "cases/:caseId", element: <CasePage /> },
          { path: "cases/:caseId/receipt", element: <ReceiptPage /> },
          { path: "cases/:caseId/material", element: <AddMaterialPage /> },
          { path: "cases/:caseId/review", element: <ReviewPage /> },
          { path: "cases/:caseId/updates", element: <UpdatePage /> },
          { path: "cases/:caseId/reviewer", element: <ReviewerPage /> },
        ],
      },
      {
        element: <RequireAuth kind="officer" />,
        children: [
          { path: "officer", element: <OfficerQueuePage /> },
          { path: "officer/cases/:caseId", element: <OfficerCasePage /> },
          { path: "officer/inbox", element: <InboxPage /> },
          { path: "officer/consultations/:id", element: <ConsultationPage /> },
        ],
      },

      {
        element: <RequireAuth kind="admin" />,
        children: [{ path: "admin", element: <AdminPage /> }, { path: "admin/officers", element: <AdminOfficersPage /> }, { path: "admin/coordinator", element: <AdminCoordinatorPage /> }],
      },

      { path: "*", element: <NotFoundPage /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <I18nProvider><RouterProvider router={router} /></I18nProvider>
  </React.StrictMode>,
);
