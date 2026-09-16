# MindEval workshop: attendee quickstart

Open the workshop URL supplied by your host (`/workshop` on the Scientific AI
platform) and connect with your team's API key. The key belongs to your team;
do not paste it into a conversation, source repository or shared screenshot.

1. Choose **Text benchmark**, a patient model and one or more clinician models.
   Select the same patient profiles for comparisons. Start with one profile and
   ten rounds; later select up to twenty profiles.
2. Start the evaluation. Each clinician/profile pair is a separate run. Up to
   five run workers execute for your team; additional work waits in a fair queue.
   Waiting for provider capacity is normal and does not mean the job is lost.
3. Open a run to follow the transcript and events. After completion, inspect the
   five judge criteria and comparison table. All clinicians in a comparison use
   the same fixed judge and original MindEval prompts.
4. Download the evidence report. It includes the exact profile, model selection,
   prompt provenance, transcript, interventions, timings and judge result.

## Try the spoken experience

Select **Spoken experience**, with Sofia as patient and Jason as clinician (or
choose other offered voices). The platform synthesizes each answer and feeds its
transcription to the other role. This evaluates the whole speech interaction;
an ASR error may alter the conversation. It is deliberately separate from the
text benchmark. Keep real patient data out of this workshop.

The transcript retains original/generated text, heard text, speech timing and
recordings. Browser playback and microphone access depend on your browser's
audio permissions; use a headset to avoid echo. A completed recording can be
replayed from its run even after reconnecting.

## Intervene

- **Pause:** stop progression while you inspect the conversation.
- **Nudge:** give an additional instruction to the selected role.
- **Take over:** become the patient or clinician. Type your message, or use the
  microphone and finish recording when you have spoken.
- **Resume model:** hand the role back and continue. After an interrupted worker,
  this is also an explicit request to retry unfinished work.
- **Abort:** end this run. It remains in the history with its partial evidence.

Every intervention is labeled. Intervened and spoken runs do not enter the
default untouched-text comparison, even when they receive a judge score.

## If something goes wrong

Closing a tab does not stop a submitted run. Reopen the workshop, connect with
the same team key and select it from **Your runs**. Keys are kept only in memory,
so a page reload asks for the key again.

A queued run is waiting, not failed. An **interrupted** run needs explicit resume;
an already-issued provider call may have consumed tokens even if its response
was lost. A **failed** run keeps its reason and evidence; tell the host the run ID
and visible error rather than repeatedly submitting duplicates. Do not share
your API key in a support message.

MindGuard is an observational classifier, not the judge or a therapist. An
unavailable observation is not a safety pass. Neither its labels nor the judge
scores establish that an AI is clinically safe or suitable for treating people.
