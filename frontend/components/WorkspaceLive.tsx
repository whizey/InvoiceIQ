"use client";

import {useCallback,useEffect,useRef,useState} from "react";
import Link from "next/link";

type Case = {
  id:string; company:string; invoice:string;
  status:string; opened:string|null;
  outstanding:string; pending:boolean;
};

type Review = {
  id:string; case_id:string; company:string;
  invoice:string; action:string;
  created:string|null; outstanding:string;
};

// Treat stored INR amounts as decimal rupees, then sum in paise.
const minor = (s:string) => {
  const m = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(String(s).trim());
  return m
    ? (m[1] ? -1 : 1) *
      (Number(m[2]) * 100 + Number((m[3] || "").padEnd(2,"0")))
    : NaN;
};

const money = (paise:number) =>
  Number.isSafeInteger(paise)
    ? new Intl.NumberFormat("en-IN",{
        style:"currency",
        currency:"INR",
        minimumFractionDigits:2,
        maximumFractionDigits:2
      }).format(paise / 100)
    : "Unavailable";

const total = (rows:{outstanding:string}[]) =>
  money(rows.reduce((sum,r) => sum + minor(r.outstanding),0));

const date = (s:string|null) =>
  s ? String(s).slice(0,10) : "Unknown";

async function api<T>(path:string,token?:string,signal?:AbortSignal):Promise<T> {
  const response = await fetch(path,{
    signal,
    cache:"no-store",
    headers:token ? {"X-InvoiceIQ-Reviewer-Key":token} : undefined
  });

  const data:unknown = await response.json().catch(() => null);

  if (!response.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data
        ? String(data.detail)
        : `HTTP ${response.status}`;

    throw Object.assign(Error(detail), {status:response.status});
  }

  return data as T;
}

function WorkspaceContent({
  type
}:{
  type:"cases"|"approvals"
}) {
  const request = useRef<AbortController | null>(null);
  const detailRequest = useRef<AbortController | null>(null);
  const [detailError,setDetailError] = useState("");
  const [cases,setCases] = useState<Case[]>([]);
  const [reviews,setReviews] = useState<Review[]>([]);
  const [query,setQuery] = useState("");
  const [mode,setMode] = useState<"board"|"table">("board");
  const [period,setPeriod] = useState("all");
  const [reviewer,setReviewer] = useState("");
  const [authorized,setAuthorized] = useState(false);
  const [selected,setSelected] = useState("");
  const [detail,setDetail] = useState<unknown>(null);
  const [loading,setLoading] = useState(type === "cases");
  const [error,setError] = useState("");
  const [ready,setReady] = useState(false);
  const [asOf,setAsOf] = useState(0);

  const load = useCallback(async (token?:string) => {
    if (type === "approvals" && !token) return;

    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError("");

    try {
      if (type === "cases") {
        const result = await api<{cases:Case[]}>(
          "/api/workspace/cases", undefined, controller.signal
        );

        if (controller.signal.aborted) return;
        if (!Array.isArray(result.cases)) {
          throw Error("Invalid cases response");
        }

        setCases(result.cases);
      } else {
        const result = await api<{reviews:Review[]}>(
          "/api/workspace/reviews",
          token, controller.signal
        );

        if (controller.signal.aborted) return;
        if (!Array.isArray(result.reviews)) {
          throw Error("Invalid approvals response");
        }

        setReviews(result.reviews);
        setAuthorized(true);
      }

      setAsOf(Date.now());
      setReady(true);
    } catch (err) {
      if (controller.signal.aborted) return;
      const denied = err instanceof Error && "status" in err && [401,403].includes(Number(err.status));
      if (denied) {
      setCases([]);
      setSelected("");
      setDetailError("");
      setDetail(null);
      detailRequest.current?.abort();
      setReady(false);
      setReviews([]);
      setAuthorized(false);
      }
      setError(
        err instanceof Error ? err.message : "Request failed"
      );

    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  },[type]);

  useEffect(() => () => {request.current?.abort();detailRequest.current?.abort();}, [type]);

  useEffect(() => {
    if (type !== "cases") return;
    const task = window.setTimeout(() => {
      void load();
    }, 0);
    return () => window.clearTimeout(task);
  },[type,load]);

  useEffect(() => {
    if (!selected) return;

    const controller = new AbortController();
    detailRequest.current = controller;

    void api<unknown>(
      `/api/workspace/cases/${encodeURIComponent(selected)}`, undefined, controller.signal
    ).then(data => {
      if (!controller.signal.aborted) setDetail(data);
    }).catch(err => {
      if (!controller.signal.aborted) setDetailError(String(err));
    });

    return () => controller.abort();
  },[selected]);

  const matches = (r:Case|Review) =>
    [
      r.company,
      r.invoice,
      "status" in r ? r.status : r.action,
      r.id
    ].join(" ").toLowerCase().includes(query.toLowerCase());

  const caseRows = cases.filter(r => {
    const recent = !!r.opened &&
      asOf - Date.parse(r.opened) <= 90*86400000 &&
      asOf >= Date.parse(r.opened);

    return matches(r) && (period === "all" || recent);
  });

  const reviewRows = reviews.filter(matches);

  const rows = type === "cases" ? caseRows : reviewRows;
  const statuses = [...new Set(caseRows.map(r => r.status))];

  function exportCsv() {
    const records = [
      ["Action ID","Company","Invoice","Action","Outstanding INR"],
      ...reviewRows.map(r => [
        r.id,r.company,r.invoice,r.action,r.outstanding
      ])
    ];

    const csv = records.map(row =>
      row.map(value => {
        const safe = /^[=+@-]/.test(value) ? "'" + value : value;
        return '"' + safe.replaceAll('"','""') + '"';
      }).join(",")
    ).join("\r\n");

    const url = URL.createObjectURL(
      new Blob([csv],{type:"text/csv;charset=utf-8"})
    );

    const link = document.createElement("a");
    link.href = url;
    link.download = "invoiceiq-reviews.csv";
    link.click();

    setTimeout(() => URL.revokeObjectURL(url),1000);
  }

  return (
    <div className="iw-shell">
      <style>{`
        .iw-shell{display:flex;flex-wrap:wrap;min-height:100vh;background:#f4f4f2;color:#111;font-family:Geist,system-ui,sans-serif}
        .iw-nav{flex:0 1 236px;background:#0b0b0b;padding:22px 14px;display:flex;flex-direction:column;gap:4px}
        .iw-nav a{color:#b5b5b5;text-decoration:none;padding:9px 10px;border-radius:6px;font-size:13px}
        .iw-nav a:hover,.iw-nav a.on{color:white;background:#222}
        .iw-main{flex:999 1 560px;min-width:0}
        .iw-head{background:#fff;padding:18px 32px;border-bottom:1px solid #e5e5e2;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px}

        /* IQ_ORIGINAL_HTML_HEADER */
        .iw-head {
          background: #FFFFFF;
          border-bottom: 1px solid #E5E5E2;
          box-shadow: none;
        }
        .iw-head h1 {
          color: #0B0B0B;
          font-size: 24px;
          font-weight: 600;
          letter-spacing: -0.02em;
        }
        .iw-head .iw-label {
          color: #6B6B6B;
          letter-spacing: 0.07em;
        }
        .iw-head .iw-btn {
          background: #FFFFFF;
          color: #0B0B0B;
          border-color: #D9D9D6;
          box-shadow: none;
        }
        .iw-head .iw-btn.on {
          background: #0B0B0B;
          color: #FFFFFF;
          border-color: #0B0B0B;
        }
        .iw-body{padding:24px 32px 48px;display:flex;flex-direction:column;gap:18px}
        .iw-row{display:flex;align-items:center;flex-wrap:wrap;gap:9px}
        .iw-card{background:white;border:1px solid #e5e5e2;border-radius:10px;padding:16px 20px}
        .iw-btn,.iw-input{border:1px solid #d9d9d6;background:#fff;color:#111;border-radius:8px;padding:9px 13px;font:inherit;font-size:13px}
        .iw-btn{cursor:pointer}
        .iw-btn.on{background:#111;color:#fff}
        .iw-btn:disabled{opacity:.5;cursor:not-allowed}
        .iw-label{color:#666;text-transform:uppercase;font-size:11px;letter-spacing:.07em}
        .iw-value{font-size:23px;font-weight:600;word-break:break-word}
        .iw-small{font-size:12px;color:#666}
        .iw-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(175px,1fr));gap:12px}
        .iw-board{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
        .iw-column{background:#ededeb;padding:12px;border-radius:10px}
        .iw-table{width:100%;border-collapse:collapse;font-size:13px}
        .iw-table th,.iw-table td{padding:11px;text-align:left;border-bottom:1px solid #eee}
        .iw-table th{font-size:11px;color:#666;text-transform:uppercase}
        @media(max-width:700px){
          .iw-nav{flex:1 1 100%;flex-direction:row;flex-wrap:wrap;padding:10px}
          .iw-head,.iw-body{padding:18px}
        }
      `}</style>

      <nav className="iw-nav" aria-label="Primary">
        <strong style={{color:"white",padding:"10px 10px 20px"}}>
          ▣ InvoiceIQ
        </strong>
        <span className="iw-label">Receivables</span>
        <Link href="/">01 Overview</Link>
        <Link href="/invoices">02 Invoice intelligence</Link>
        <span className="iw-label">Recovery</span>
        <Link href="/cases" className={type==="cases"?"on":""}>
          03 Recovery cases
        </Link>
        <Link href="/approvals" className={type==="approvals"?"on":""}>
          05 Approvals
        </Link>
      </nav>

      <main className="iw-main" aria-busy={loading}>
        <header className="iw-head">
          <div>
            <div className="iw-label">Recovery</div>
            <h1 style={{margin:0,fontSize:24}}>
              {type==="cases"?"Recovery cases":"Approvals"}
            </h1>
          </div>

          <div className="iw-row">
            <button
              className="iw-btn"
              disabled={loading || (type==="approvals"&&!authorized)}
              onClick={() => void load(
                type==="approvals"?reviewer:undefined
              )}
            >
              Refresh
            </button>

            {type==="cases" && (
              <>
                <button
                  className={`iw-btn ${mode==="board"?"on":""}`}
                  onClick={() => setMode("board")}
                >
                  Board
                </button>
                <button
                  className={`iw-btn ${mode==="table"?"on":""}`}
                  onClick={() => setMode("table")}
                >
                  Table
                </button>
              </>
            )}
          </div>
        </header>

        <div className="iw-body">
          <div className="iw-row">
            <input
              className="iw-input"
              aria-label="Search records"
              placeholder="Search company, invoice, status"
              value={query}
              onChange={e => setQuery(e.target.value)}
            />

            {type==="cases" ? (
              <select
                className="iw-input"
                aria-label="Opened date"
                value={period}
                onChange={e => setPeriod(e.target.value)}
              >
                <option value="all">Opened: All dates</option>
                <option value="90">Opened: Last 90 days</option>
              </select>
            ) : (
              <>
                <input
                  className="iw-input"
                  type="password"
                  autoComplete="off"
                  aria-label="Reviewer credential"
                  placeholder="Reviewer credential"
                  value={reviewer}
                  onChange={e => {
                    request.current?.abort();
                    detailRequest.current?.abort();
                    setLoading(false);
                    setError("");
                    setSelected("");
                    setDetailError("");
                    setDetail(null);
                    setReviewer(e.target.value);
                    setAuthorized(false);
                    setReady(false);
                    setReviews([]);
                  }}
                />

                <button
                  className="iw-btn on"
                  disabled={!reviewer.trim()||loading}
                  onClick={() => void load(reviewer.trim())}
                >
                  Load reviews
                </button>

                <button
                  className="iw-btn"
                  disabled={!authorized||!reviewRows.length}
                  onClick={exportCsv}
                >
                  Export CSV
                </button>
              </>
            )}
          </div>

          {loading && (
            <p role="status" className="iw-small">
              Loading real records…
            </p>
          )}

          {error && (
            <p role="alert" className="iw-card" style={{color:"#a11"}}>
              {error}
            </p>
          )}

          {type==="approvals"&&!authorized && (
            <p className="iw-card">
              Enter an authorized reviewer credential.
              No sample approvals or balances are shown.
            </p>
          )}

          <section className="iw-kpis" aria-label="Live totals">
            <div className="iw-card">
              <div className="iw-label">
                {type==="cases"?"Cases displayed":"Pending reviews"}
              </div>
              <div className="iw-value">
                {ready?rows.length:"—"}
              </div>
            </div>

            <div className="iw-card">
              <div className="iw-label">Outstanding exposure</div>
              <div className="iw-value">
                {ready?total(rows):"—"}
              </div>
              <p className="iw-small">
                Real recorded amounts · INR
              </p>
            </div>

            {type==="cases" && (
              <div className="iw-card">
                <div className="iw-label">Cases awaiting approval</div>
                <div className="iw-value">
                  {ready?caseRows.filter(r=>r.pending).length:"—"}
                </div>
              </div>
            )}
          </section>

          {type==="cases"&&mode==="board" ? (
            <section
              aria-label="Live case board"
              className="iw-board"
            >
              {statuses.map(status => (
                <div className="iw-column" key={status}>
                  <strong>
                    {status.replaceAll("_"," ")} · {
                      caseRows.filter(r=>r.status===status).length
                    }
                  </strong>

                  {caseRows.filter(r=>r.status===status).map(r => (
                    <button
                      key={r.id}
                      className="iw-card iw-btn"
                      style={{
                        display:"block",
                        width:"100%",
                        textAlign:"left",
                        whiteSpace:"normal",
                        marginTop:10
                      }}
                      onClick={() => {
                        if (selected === r.id) return;
                        setDetailError("");
                        setDetail(null);
                        setSelected(r.id);
                      }}
                    >
                      <div className="iw-small">{r.invoice}</div>
                      <b>{r.company}</b>
                      <div className="iw-value">
                        {money(minor(r.outstanding))}
                      </div>
                      <div className="iw-small">
                        Opened {date(r.opened)}
                        {r.pending?" · Approval pending":""}
                      </div>
                    </button>
                  ))}
                </div>
              ))}

              {ready&&!loading&&!error&&!caseRows.length && (
                <p className="iw-card">No matching cases.</p>
              )}
            </section>
          ) : (
            <section className="iw-card" style={{overflowX:"auto"}}>
              <table className="iw-table">
                <thead>
                  <tr>
                    {[
                      "Company","Invoice","Status / action",
                      "Outstanding","Date","Details"
                    ].map(title => <th key={title}>{title}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {rows.map(r => (
                    <tr key={r.id}>
                      <td>{r.company}</td>
                      <td>{r.invoice}</td>
                      <td>{"status" in r?r.status:r.action}</td>
                      <td>{money(minor(r.outstanding))}</td>
                      <td>{date("opened" in r?r.opened:r.created)}</td>
                      <td>
                        <button
                          className="iw-btn"
                          onClick={() => {
                            const id = "case_id" in r ? r.case_id : r.id;
                            if (selected === id) return;
                            setDetailError("");
                            setDetail(null);
                            setSelected(id);
                          }}
                        >
                          Details
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {ready&&!loading&&!error&&!rows.length && (
                <p className="iw-small">
                  No records in this selection.
                </p>
              )}
            </section>
          )}

          {selected && (
            <section className="iw-card">
              <div
                className="iw-row"
                style={{justifyContent:"space-between"}}
              >
                <h2 style={{fontSize:16}}>Case evidence</h2>
                <button
                  className="iw-btn"
                  onClick={() => {
                    detailRequest.current?.abort();
                    setDetailError("");
                    setDetail(null);
                    setSelected("");
                  }}
                >
                  Close
                </button>
              </div>

              {detailError ? <p role="alert">{detailError}</p> : detail ? (
                <pre style={{
                  whiteSpace:"pre-wrap",
                  overflowWrap:"anywhere",
                  fontSize:12,
                  maxHeight:420,
                  overflow:"auto"
                }}>
                  {JSON.stringify(detail,null,2)}
                </pre>
              ) : (
                <p>Loading case evidence…</p>
              )}
            </section>
          )}

          <p className="iw-small">
            {type==="cases"
              ? "Risk sweep remains unavailable until workspace authorization is verified."
              : "Read-only reviewer queue. Approval decisions require a separately verified write endpoint."}
          </p>
        </div>
      </main>
    </div>
  );
}

export default function Workspace({type}:{type:"cases"|"approvals"}) {
  return <WorkspaceContent key={type} type={type} />;
}
