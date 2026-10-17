"use client";

import Link from "next/link";

export default function RouteError({reset}:{error:Error&{digest?:string};reset:()=>void}){
  return <main className="route-error" role="alert">
    <p className="settings-eyebrow">InvoiceIQ / Page unavailable</p>
    <h1>We couldn’t load this page.</h1>
    <p>Check your connection and try again. If the problem continues, the service may be temporarily unavailable.</p>
    <div><button type="button" onClick={reset}>Try again</button><Link href="/home">Return home</Link></div>
  </main>;
}
