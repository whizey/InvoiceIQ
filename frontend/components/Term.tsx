"use client";
import {useId} from "react";
import {GLOSSARY, type GlossaryKey} from "@/lib/glossary";

/** A business term with its plain-language meaning on hover, keyboard focus or tap. */
export default function Term({k, children}:{k:GlossaryKey; children:React.ReactNode}){
  const id=useId();
  return <span className="term" tabIndex={0} aria-describedby={id}>
    {children}
    <span role="tooltip" id={id} className="term-tip">{GLOSSARY[k]}</span>
  </span>;
}
