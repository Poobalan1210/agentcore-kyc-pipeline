import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@shared/components/Button";
import { Icon } from "../console/icons";
import { CAPTURE_COPY } from "./copy";
import { LivenessStage } from "./LivenessStage";

/**
 * The photo of the ID, taken with the laptop's webcam, on the same full-window stage as the
 * selfie: the applicant holds the document up to the camera, fits it in the frame, takes the
 * photo and checks it before using it.
 *
 * Only the inside of the frame is kept. `rekognition_compare_faces` compares the largest face in
 * the document image with the selfie, so a photo that also caught the applicant's own face around
 * the edges could be compared with the wrong face; cropping to the frame keeps the document's
 * portrait the only face in it. The guide has the generated specimens' proportions (their card is
 * 520 x 430 points), which is what makes "fill the frame" mean "fill it with the document".
 *
 * The preview is not mirrored: the audience reads the document the right way round.
 */

/** Width over height of the demo documents' card (`demo-assets/gen_documents.py` CARD_W/CARD_H). */
const GUIDE_ASPECT = 520 / 430;
/** How much of the frame's width the guide takes: on a 16:9 camera, about 80% of its height. */
const GUIDE_WIDTH = 0.56;
/** Kept around the guide when cropping, for a document held a little off the lines. */
const CROP_MARGIN = 0.03;
const JPEG_QUALITY = 0.92;

type Phase = "starting" | "live" | "review" | "error";

export function DocumentCapture({
  documentType,
  onCapture,
  onCancel,
  onUploadInstead,
}: {
  documentType: string;
  onCapture: (file: File) => void;
  onCancel: () => void;
  onUploadInstead: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const guideRef = useRef<HTMLDivElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [phase, setPhase] = useState<Phase>("starting");
  const [aspect, setAspect] = useState(16 / 9);
  const [shot, setShot] = useState<{ blob: Blob; url: string } | null>(null);

  // The camera runs while the stage is open and stops the moment it closes, so the selfie step
  // that follows can have it.
  useEffect(() => {
    let cancelled = false;
    if (!navigator.mediaDevices?.getUserMedia) {
      setPhase("error");
      return;
    }
    navigator.mediaDevices
      .getUserMedia({
        video: { width: { ideal: 1920 }, height: { ideal: 1080 }, facingMode: "user" },
        audio: false,
      })
      .then((stream) => {
        if (cancelled) {
          stream.getTracks().forEach((t) => t.stop());
          return;
        }
        streamRef.current = stream;
        if (videoRef.current) videoRef.current.srcObject = stream;
      })
      .catch((err) => {
        // eslint-disable-next-line no-console
        console.error("document camera", err);
        if (!cancelled) setPhase("error");
      });
    return () => {
      cancelled = true;
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    };
  }, []);

  // Each photo's preview URL lives exactly as long as the photo is on screen.
  useEffect(() => {
    if (!shot) return;
    return () => URL.revokeObjectURL(shot.url);
  }, [shot]);

  const take = useCallback(() => {
    const video = videoRef.current;
    const guide = guideRef.current;
    if (!video || !guide || !video.videoWidth) return;
    const v = video.getBoundingClientRect();
    const g = guide.getBoundingClientRect();
    // The video fills a box of its own aspect ratio, so screen pixels map linearly onto it.
    const sx = video.videoWidth / v.width;
    const sy = video.videoHeight / v.height;
    const left = Math.max(0, (g.left - v.left - g.width * CROP_MARGIN) * sx);
    const top = Math.max(0, (g.top - v.top - g.height * CROP_MARGIN) * sy);
    const right = Math.min(video.videoWidth, (g.right - v.left + g.width * CROP_MARGIN) * sx);
    const bottom = Math.min(video.videoHeight, (g.bottom - v.top + g.height * CROP_MARGIN) * sy);
    const width = Math.round(right - left);
    const height = Math.round(bottom - top);
    if (width < 2 || height < 2) return;
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.drawImage(video, left, top, width, height, 0, 0, width, height);
    canvas.toBlob(
      (blob) => {
        if (!blob) return;
        setShot({ blob, url: URL.createObjectURL(blob) });
        setPhase("review");
      },
      "image/jpeg",
      JPEG_QUALITY,
    );
  }, []);

  function use() {
    if (!shot) return;
    const name = `${documentType.toLowerCase()}-photo-${Date.now()}.jpg`;
    onCapture(new File([shot.blob], name, { type: "image/jpeg" }));
  }

  function retake() {
    setShot(null);
    setPhase("live");
  }

  // Space takes the photo: easier than aiming a pointer while holding a document up.
  useEffect(() => {
    if (phase !== "live") return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === " " && !(event.target instanceof HTMLButtonElement)) {
        event.preventDefault();
        take();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [phase, take]);

  const card = documentType !== "PASSPORT";

  return (
    <LivenessStage
      label="Photo of your ID"
      live={phase === "live"}
      lens={phase === "starting" || phase === "live"}
      lensText={CAPTURE_COPY.lens}
      onEscape={onCancel}
      footer={
        <>
          <div className="lv-stage__copy">
            <span className="lv-stage__eyebrow">
              <Icon name="document" size={14} /> {CAPTURE_COPY.eyebrow}
            </span>
            {phase === "error" ? null : (
              <h2 className="lv-stage__title">
                {phase === "review" ? CAPTURE_COPY.checkTitle : CAPTURE_COPY.title}
              </h2>
            )}
            <p className="lv-stage__lead">
              {phase === "error"
                ? CAPTURE_COPY.cameraError
                : phase === "review"
                  ? CAPTURE_COPY.checkLead
                  : CAPTURE_COPY.lead}
            </p>
          </div>
          <div className="lv-stage__actions">
            {phase === "review" ? (
              <>
                <Button variant="primary" onClick={use}>
                  {CAPTURE_COPY.use}
                </Button>
                <Button variant="secondary" onClick={retake}>
                  {CAPTURE_COPY.retake}
                </Button>
              </>
            ) : phase === "live" ? (
              <Button variant="primary" onClick={take}>
                <Icon name="document" size={16} /> {CAPTURE_COPY.take}
              </Button>
            ) : null}
            <Button variant="ghost" onClick={onUploadInstead}>
              {CAPTURE_COPY.uploadInstead}
            </Button>
            <Button variant="ghost" onClick={onCancel}>
              {CAPTURE_COPY.cancel}
            </Button>
          </div>
        </>
      }
    >
      <div className="dc-view" style={{ aspectRatio: String(aspect) }}>
        <video
          ref={videoRef}
          className="dc-video"
          autoPlay
          muted
          playsInline
          onLoadedMetadata={(e) => {
            const video = e.currentTarget;
            if (video.videoWidth && video.videoHeight) setAspect(video.videoWidth / video.videoHeight);
            setPhase((p) => (p === "starting" ? "live" : p));
          }}
        />
        {phase === "review" && shot ? (
          <div className="dc-shot">
            <img src={shot.url} alt="The photo of your ID" />
          </div>
        ) : null}
        {phase === "starting" || phase === "live" ? (
          <div className="dc-guide-layer" aria-hidden="true">
            <div
              ref={guideRef}
              className="dc-guide"
              style={{ width: `${GUIDE_WIDTH * 100}%`, aspectRatio: String(GUIDE_ASPECT) }}
            >
              <span className="dc-guide__label">
                {card ? CAPTURE_COPY.guideCard : CAPTURE_COPY.guidePassport}
              </span>
              <i className="dc-corner dc-corner--tl" />
              <i className="dc-corner dc-corner--tr" />
              <i className="dc-corner dc-corner--bl" />
              <i className="dc-corner dc-corner--br" />
            </div>
          </div>
        ) : null}
        {phase === "starting" ? (
          <div className="dc-status">
            <span className="lv-panel__spinner" aria-hidden="true" />
            <span>{CAPTURE_COPY.starting}</span>
          </div>
        ) : null}
        {phase === "error" ? (
          <div className="dc-status dc-status--error" role="alert">
            <Icon name="alert" size={40} />
          </div>
        ) : null}
      </div>
    </LivenessStage>
  );
}
