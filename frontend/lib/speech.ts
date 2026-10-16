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

const MAX_RECORDING_MS=20000;
const SILENCE_MS=1500;      // stop this long after the speaker goes quiet
const SPEECH_LEVEL=0.025;   // RMS above this counts as speech
const NO_SPEECH_MS=7000;    // give up if nothing is said

function recorderMime(){
  if(typeof MediaRecorder==="undefined")return"";
  for(const type of["audio/webm;codecs=opus","audio/webm","audio/mp4","audio/ogg;codecs=opus"]){
    if(MediaRecorder.isTypeSupported(type))return type;
  }
  return"";
}

/**
 * Speech-to-text. Uses the browser's own recognition first; when that service is unreachable
 * (common in Brave, some Chromium builds, VPNs and firewalls) or missing, it records the
 * microphone locally and has InvoiceIQ's API transcribe the clip instead.
 */
export function useSpeechToText(onText:(text:string,final:boolean)=>void){
  const [supported,setSupported]=useState(false);
  const [listening,setListening]=useState(false);
  const [transcribing,setTranscribing]=useState(false);
  const [error,setError]=useState("");
  const ref=useRef<Recognition|null>(null);
  const recorder=useRef<MediaRecorder|null>(null);
  const stream=useRef<MediaStream|null>(null);
  const timer=useRef<number|undefined>(undefined);
  const serverMode=useRef(false);
  const handler=useRef(onText);
  useEffect(()=>{handler.current=onText},[onText]);
  const releaseMic=useCallback(()=>{
    window.clearTimeout(timer.current);
    stream.current?.getTracks().forEach(track=>track.stop());
    stream.current=null;
  },[]);
  useEffect(()=>{
    const w=window as unknown as {SpeechRecognition?:RecognitionCtor;webkitSpeechRecognition?:RecognitionCtor};
    const t=window.setTimeout(()=>{
      const canRecord=Boolean(navigator.mediaDevices?.getUserMedia)&&Boolean(recorderMime());
      setSupported(Boolean(w.SpeechRecognition||w.webkitSpeechRecognition)||canRecord);
    },0);
    return()=>{window.clearTimeout(t);ref.current?.abort();if(recorder.current?.state==="recording")recorder.current.stop();releaseMic()};
  },[releaseMic]);

  const startRecording=useCallback(async()=>{
    const mime=recorderMime();
    if(!mime||!navigator.mediaDevices?.getUserMedia){setError("Voice input is not available in this browser.");return}
    try{
      const media=await navigator.mediaDevices.getUserMedia({audio:true});
      stream.current=media;
      const chunks:Blob[]=[];
      const rec=new MediaRecorder(media,{mimeType:mime,audioBitsPerSecond:24000});
      recorder.current=rec;
      rec.ondataavailable=e=>{if(e.data.size>0)chunks.push(e.data)};
      rec.onstop=async()=>{
        releaseMic();setListening(false);recorder.current=null;
        const blob=new Blob(chunks,{type:mime});
        if(blob.size<1500){setError("No speech was recorded. Try again.");return}
        setTranscribing(true);
        try{
          const buffer=new Uint8Array(await blob.arrayBuffer());
          let binary="";
          for(let i=0;i<buffer.length;i+=0x8000)binary+=String.fromCharCode(...buffer.subarray(i,i+0x8000));
          const response=await fetch("/api/portal/transcribe",{method:"POST",headers:{"Content-Type":"application/json"},
            body:JSON.stringify({audio:btoa(binary),mime:mime.split(";")[0]})});
          const body=await response.json();
          if(!response.ok)throw new Error(body.detail||"Transcription failed");
          if(body.text)handler.current(body.text,true);else setError("No speech was detected. Try again.");
        }catch(e){setError(e instanceof Error?e.message:"Transcription failed")}
        finally{setTranscribing(false)}
      };
      setError("");setListening(true);rec.start();
      // Stop automatically once speech has started and then gone quiet.
      try{
        const Ctx=window.AudioContext||(window as unknown as {webkitAudioContext:typeof AudioContext}).webkitAudioContext;
        const audio=new Ctx();const analyser=audio.createAnalyser();analyser.fftSize=1024;
        audio.createMediaStreamSource(media).connect(analyser);
        const data=new Uint8Array(analyser.fftSize);
        const began=Date.now();let spoke=false;let lastVoice=began;
        const watch=window.setInterval(()=>{
          if(rec.state!=="recording"){window.clearInterval(watch);void audio.close();return}
          analyser.getByteTimeDomainData(data);
          let sum=0;for(const v of data){const x=(v-128)/128;sum+=x*x}
          const now=Date.now();
          if(Math.sqrt(sum/data.length)>SPEECH_LEVEL){spoke=true;lastVoice=now}
          if((spoke&&now-lastVoice>SILENCE_MS)||(!spoke&&now-began>NO_SPEECH_MS)){window.clearInterval(watch);void audio.close();rec.stop()}
        },120);
      }catch{/* no analyser available: the tap-to-stop and time limit still apply */}
      timer.current=window.setTimeout(()=>{if(rec.state==="recording")rec.stop()},MAX_RECORDING_MS);
    }catch(e){
      releaseMic();setListening(false);
      setError(e instanceof DOMException&&e.name==="NotAllowedError"
        ?MESSAGES["not-allowed"]:"No microphone was found or it could not be opened.");
    }
  },[releaseMic]);

  const start=useCallback(()=>{
    if(ref.current||recorder.current||transcribing)return;
    const w=window as unknown as {SpeechRecognition?:RecognitionCtor;webkitSpeechRecognition?:RecognitionCtor};
    const Ctor=w.SpeechRecognition||w.webkitSpeechRecognition;
    if(!Ctor||serverMode.current){void startRecording();return}
    const rec=new Ctor();
    rec.lang="en-IN";rec.interimResults=true;rec.continuous=false;rec.maxAlternatives=1;
    rec.onresult=e=>{
      let text="";let final=false;
      for(let i=e.resultIndex;i<e.results.length;i++){text+=e.results[i][0].transcript;if(e.results[i].isFinal)final=true}
      handler.current(text.trim(),final);
    };
    rec.onerror=e=>{
      if(e.error==="aborted")return;
      if(e.error==="network"||e.error==="service-not-allowed"){
        // The browser's recognition service is unreachable: switch to server transcription and record right away.
        serverMode.current=true;ref.current=null;setListening(false);void startRecording();return;
      }
      setError(MESSAGES[e.error]??"Voice input failed. Please try again.");
    };
    rec.onend=()=>{ref.current=null;if(!serverMode.current||!recorder.current)setListening(false)};
    setError("");ref.current=rec;setListening(true);
    try{rec.start()}catch{ref.current=null;setListening(false);setError("Voice input could not start.")}
  },[startRecording,transcribing]);

  const stop=useCallback(()=>{
    if(recorder.current?.state==="recording")recorder.current.stop();
    else ref.current?.stop();
  },[]);
  return{supported,listening,transcribing,error,start,stop};
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
