"use client";

import { useRef, useState } from "react";
import { startVoiceCallAction, submitVoiceTurnAction } from "@/app/actions";

type TranscriptEntry = { speaker: "agent" | "customer"; text: string };

type CallState = "idle" | "in_call" | "ended";

export function VoiceCallPanel({ caseId, disabled, disabledReason }: {
  caseId: string;
  disabled?: boolean;
  disabledReason?: string;
}) {
  const [state, setState] = useState<CallState>("idle");
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  const [turnNumber, setTurnNumber] = useState(0);
  const [startedAt, setStartedAt] = useState<string | null>(null);
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<{ action_type: string; policy_decision: string; reason: string } | null>(
    null,
  );
  const [typedReply, setTypedReply] = useState("");
  const [isBusy, setIsBusy] = useState(false);
  const [isRecording, setIsRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  function playAudioIfAny(base64: string | null) {
    if (!base64) {
      setAudioUrl(null);
      return;
    }
    const url = `data:audio/mp3;base64,${base64}`;
    setAudioUrl(url);
  }

  async function handleStart() {
    setError(null);
    setIsBusy(true);
    try {
      const result = await startVoiceCallAction(caseId);
      if (!result.ok) {
        setError(result.error);
        return;
      }
      if (!result.started) {
        setError(result.reason ?? "Call could not start.");
        setState("ended");
        return;
      }
      setState("in_call");
      setTurnNumber(1);
      setStartedAt(new Date().toISOString());
      setTranscript(result.agent_line ? [{ speaker: "agent", text: result.agent_line }] : []);
      playAudioIfAny(result.agent_audio_base64);
      if (result.ended) setState("ended");
    } finally {
      setIsBusy(false);
    }
  }

  async function submitTurn(formExtra: (fd: FormData) => void) {
    setError(null);
    setIsBusy(true);
    try {
      const fd = new FormData();
      fd.set("turn_number", String(turnNumber + 1));
      fd.set("started_at", startedAt ?? new Date().toISOString());
      fd.set("transcript_so_far", JSON.stringify(transcript));
      formExtra(fd);

      const result = await submitVoiceTurnAction(caseId, fd);
      if (!result.ok) {
        setError(result.error);
        return;
      }
      setTurnNumber(result.turn_number);
      setTranscript((prev) => [
        ...prev,
        { speaker: "customer", text: result.transcript_user },
        ...(result.agent_line ? [{ speaker: "agent" as const, text: result.agent_line }] : []),
      ]);
      playAudioIfAny(result.agent_audio_base64);
      if (result.outcome) setOutcome(result.outcome);
      if (result.ended) setState("ended");
    } finally {
      setIsBusy(false);
    }
  }

  async function handleRecord() {
    if (isRecording) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      chunksRef.current = [];
      recorder.ondataavailable = (e) => chunksRef.current.push(e.data);
      recorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunksRef.current, { type: recorder.mimeType || "audio/webm" });
        void submitTurn((fd) => fd.set("audio", blob, "reply.webm"));
      };
      mediaRecorderRef.current = recorder;
      recorder.start();
      setIsRecording(true);
    } catch {
      setError("Microphone unavailable or permission denied — type your reply below instead.");
    }
  }

  function handleStopRecording() {
    mediaRecorderRef.current?.stop();
    setIsRecording(false);
  }

  async function handleSendTyped() {
    if (!typedReply.trim()) return;
    const text = typedReply;
    setTypedReply("");
    await submitTurn((fd) => fd.set("typed_reply", text));
  }

  function handleHangUp() {
    // Client-only — no backend call, no persisted session to close.
    setState("ended");
  }

  function reset() {
    setState("idle");
    setTranscript([]);
    setTurnNumber(0);
    setStartedAt(null);
    setAudioUrl(null);
    setOutcome(null);
    setError(null);
  }

  if (disabled) {
    return (
      <div className="rounded-lg border border-dashed border-slate-200 p-4 text-sm text-slate-400">
        Hinglish recovery call unavailable — {disabledReason}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {state === "idle" && (
        <button
          type="button"
          disabled={isBusy}
          onClick={handleStart}
          className="rounded-lg bg-white px-4 py-2 text-sm font-medium text-slate-700 shadow-sm ring-1 ring-inset ring-slate-300 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-400"
        >
          {isBusy ? "Starting…" : "Start Hinglish recovery call"}
        </button>
      )}

      {state !== "idle" && (
        <div className="rounded-lg border border-slate-200 p-3">
          <div className="flex items-center justify-between">
            <p className="text-xs font-medium uppercase tracking-wide text-slate-400">
              Turn {turnNumber} of 4
            </p>
            {state === "in_call" && (
              <button type="button" onClick={handleHangUp} className="text-xs text-rose-600 hover:underline">
                Hang up
              </button>
            )}
          </div>

          <ul className="mt-2 space-y-2 text-sm">
            {transcript.map((entry, i) => (
              <li key={i} className={entry.speaker === "agent" ? "text-slate-900" : "text-indigo-700"}>
                <span className="font-medium">{entry.speaker === "agent" ? "Agent: " : "You: "}</span>
                {entry.text}
              </li>
            ))}
          </ul>

          {audioUrl && <audio className="mt-2 w-full" controls autoPlay src={audioUrl} />}
          {!audioUrl && state === "in_call" && (
            <p className="mt-2 text-xs text-slate-400">🔇 Simulated — no Sarvam key configured, text only.</p>
          )}

          {outcome && (
            <p className="mt-3 rounded-lg bg-slate-50 p-2 text-xs text-slate-600">
              Call ended — policy {outcome.policy_decision.toLowerCase()} {outcome.action_type}: {outcome.reason}
            </p>
          )}

          {state === "in_call" && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {isRecording ? (
                <button
                  type="button"
                  onClick={handleStopRecording}
                  className="rounded-lg bg-rose-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-rose-500"
                >
                  ⏹ Stop recording
                </button>
              ) : (
                <button
                  type="button"
                  disabled={isBusy}
                  onClick={handleRecord}
                  className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-500 disabled:cursor-not-allowed disabled:bg-indigo-300"
                >
                  🎤 Record reply
                </button>
              )}
              <span className="text-xs text-slate-400">or type (e.g. mic unavailable):</span>
              <input
                type="text"
                value={typedReply}
                onChange={(e) => setTypedReply(e.target.value)}
                placeholder="haan, kal pay kar dunga"
                disabled={isBusy}
                className="min-w-0 flex-1 rounded-lg border border-slate-300 px-2 py-1 text-xs"
              />
              <button
                type="button"
                disabled={isBusy || !typedReply.trim()}
                onClick={handleSendTyped}
                className="rounded-lg bg-white px-3 py-1.5 text-xs font-medium text-slate-700 shadow-sm ring-1 ring-inset ring-slate-300 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-400"
              >
                Send
              </button>
            </div>
          )}

          {state === "ended" && (
            <button
              type="button"
              onClick={reset}
              className="mt-3 text-xs text-indigo-600 hover:underline"
            >
              Start another call
            </button>
          )}
        </div>
      )}

      {error && <p className="text-xs text-rose-600">{error}</p>}
    </div>
  );
}
