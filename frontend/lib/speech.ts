"use client";

import {useCallback, useEffect, useRef, useState} from "react";

// Minimal typings: the Web Speech API is not in TypeScript's DOM lib.
type RecognitionResult={isFinal:boolean;0:{transcript:string}};
type RecognitionEvent={resultIndex:number;results:ArrayLike<RecognitionResult>};
type Recognition={lang:string;interimResults:boolean;continuous:boolean;maxAlternatives:number;
  onresult:((e:RecognitionEvent)=>void)|null;onerror:((e:{error:string})=>void)|null;onend:(()=>void)|null;
  start():void;stop():void;abort():void};
type RecognitionCtor=new()=>Recognition;

const MESSAGES:Record<string,string>={
  "not-allowed":"Microphone access was blocked. Allow it in your browser's site settings to use voice input.",
  "service-not-allowed":"Voice input is not allowed in this browser.",
  "no-speech":"No speech was detected. Try again.",
  "audio-capture":"No microphone was found.",
  "network":"Voice recognition needs a network connection.",
};

/** Browser speech-to-text. `supported` is false (and the UI hides itself) where unavailable, e.g. Firefox. */
export function useSpeechToText(onText:(text:string,final:boolean)=>void){
  const [supported,setSupported]=useState(false);
  const [listening,setListening]=useState(false);
  const [error,setError]=useState("");
  const ref=useRef<Recognition|null>(null);
  const handler=useRef(onText);
  useEffect(()=>{handler.current=onText},[onText]);
  useEffect(()=>{
    const w=window as unknown as {SpeechRecognition?:RecognitionCtor;webkitSpeechRecognition?:RecognitionCtor};
    const timer=window.setTimeout(()=>setSupported(Boolean(w.SpeechRecognition||w.webkitSpeechRecognition)),0);
    return()=>{window.clearTimeout(timer);ref.current?.abort()};
  },[]);
  const start=useCallback(()=>{
    const w=window as unknown as {SpeechRecognition?:RecognitionCtor;webkitSpeechRecognition?:RecognitionCtor};
    const Ctor=w.SpeechRecognition||w.webkitSpeechRecognition;
    if(!Ctor||ref.current)return;
    const rec=new Ctor();
    rec.lang="en-IN";rec.interimResults=true;rec.continuous=false;rec.maxAlternatives=1;
    rec.onresult=e=>{
      let text="";let final=false;
      for(let i=e.resultIndex;i<e.results.length;i++){text+=e.results[i][0].transcript;if(e.results[i].isFinal)final=true}
      handler.current(text.trim(),final);
    };
    rec.onerror=e=>{if(e.error!=="aborted")setError(MESSAGES[e.error]??"Voice input failed. Please try again.")};
    rec.onend=()=>{ref.current=null;setListening(false)};
    setError("");ref.current=rec;setListening(true);
    try{rec.start()}catch{ref.current=null;setListening(false);setError("Voice input could not start.")}
  },[]);
  const stop=useCallback(()=>{ref.current?.stop()},[]);
  return{supported,listening,error,start,stop};
}

/** Browser text-to-speech with plain-text cleanup. */
export function useTextToSpeech(){
  const [supported,setSupported]=useState(false);
  const [speaking,setSpeaking]=useState(false);
  useEffect(()=>{
    const timer=window.setTimeout(()=>setSupported("speechSynthesis" in window),0);
    return()=>{window.clearTimeout(timer);if("speechSynthesis" in window)window.speechSynthesis.cancel()};
  },[]);
  const stop=useCallback(()=>{if("speechSynthesis" in window)window.speechSynthesis.cancel();setSpeaking(false)},[]);
  const speak=useCallback((text:string)=>{
    if(!("speechSynthesis" in window))return;
    window.speechSynthesis.cancel();
    const clean=text.replace(/[*_`#>]/g,"").replace(/₹\s?([\d,.]+)\s?Cr/gi,"rupees $1 crore").replace(/₹\s?([\d,.]+)\s?L/gi,"rupees $1 lakh").replace(/₹/g,"rupees ");
    const utterance=new SpeechSynthesisUtterance(clean);
    utterance.lang="en-IN";utterance.rate=1;
    utterance.onend=()=>setSpeaking(false);utterance.onerror=()=>setSpeaking(false);
    setSpeaking(true);window.speechSynthesis.speak(utterance);
  },[]);
  return{supported,speaking,speak,stop};
}
