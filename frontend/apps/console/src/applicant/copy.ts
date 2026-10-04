import type { AccountState, ApplicantStatus, TransactionType } from "@shared/api/client";

/**
 * Every sentence the applicant reads, from the welcome screen to the account.
 *
 * Held to the same rule as the messages the agents write: nothing here may say *why* an
 * application or an account is limited. No screening, no sanctions, no risk, no compliance team.
 * Telling a customer they are under that kind of review can itself be an offence (tipping off),
 * and the safest wording is fixed wording that says what the customer can do and when they will
 * hear more.
 *
 * Kept in one file so one test can hold all of it to that rule: `api/tests/test_notifications.py`
 * runs the API's own tipping-off guard over every string literal here. Put applicant-facing
 * text in this file, not inline in a component, or the test cannot see it.
 */

/** The bank in the demo. AnyCompany is the fictitious company name AWS uses in its own material. */
export const BANK_NAME = "AnyCompany Bank";

export interface Copy {
  title: string;
  body: string;
}

// ------------------------------------------------------------------------------- onboarding

export const WELCOME_COPY = {
  title: "Open an account in minutes",
  lead:
    "A few details, a photo of your ID and a short selfie video. Most applications are " +
    "decided in under 90 seconds.",
  needHeading: "What you'll need",
  needDocument: "A passport, driver's license or national ID card",
  needCamera: "A camera, for a photo of your ID and a selfie video",
  needTime: "About three minutes",
  countryLabel: "Where do you live?",
  productLabel: "Choose an account",
  consent:
    "I agree to the Terms and the Privacy Notice, and to my face being used to confirm that " +
    "I'm a real person and that my ID is mine.",
  noticeToggle: "How we use your face",
  // D-14: every deletion claim is scoped to our copy. Rekognition keeps its own reference
  // images, and this demo account has not opted out of their service-improvement use.
  notice:
    "The selfie video goes straight to Amazon Rekognition, which confirms that a real person " +
    "is in front of the camera. We keep one still image from it, only to compare with the photo " +
    "on your ID, and our copy is deleted automatically after about a day. This demo has not " +
    "opted out of AWS using these images to improve its services, so AWS may keep its own copy.",
  start: "Get started",
};

export const PRODUCT_COPY: Record<string, Copy> = {
  current_account: { title: "Checking account", body: "For everyday spending, with a debit card" },
  savings_account: { title: "Savings account", body: "For putting money aside" },
};

export const DETAILS_COPY = {
  title: "About you",
  lead: "Enter your details exactly as they appear on your ID. We compare the two.",
  givenNames: "First and middle names",
  surname: "Last name",
  dateOfBirth: "Date of birth",
  nationality: "Nationality",
  address: "Home address",
  missing: "Please add your",
  continue: "Continue",
};

export const DOCUMENT_COPY = {
  title: "Add your ID",
  lead: "We read your details from it, and compare its photo with your selfie.",
  typeLabel: "Which document?",
  issuerLabel: "Issued by",
  takePhoto: "Take a photo",
  upload: "Upload a photo instead",
  retake: "Retake",
  replace: "Choose another",
  previewHint: "Every detail should be sharp, with nothing hidden by glare.",
  continue: "Continue",
  uploading: "Uploading securely…",
  capturing: "Photographing your ID",
  capturingHint: "Hold it up to the camera and keep it still.",
  // A PDF is accepted (R23.1), but neither the text reader nor the face comparison can open one,
  // so the application can only be decided by a person. Said before submitting, while the
  // applicant can still pick the photo instead.
  pdfSlowPath:
    "A PDF can't be read automatically, so a person will need to check this application, " +
    "which takes longer. For a decision in seconds, upload a JPEG or PNG photo of the page instead.",
};

export const DOCUMENT_TYPE_COPY: Record<string, Copy> = {
  PASSPORT: { title: "Passport", body: "The photo page" },
  DRIVERS_LICENSE: { title: "Driver's license", body: "The front of the card" },
  NATIONAL_ID: { title: "National ID card", body: "The front of the card" },
};

/** The camera stage for the ID: shown on the laptop, where the webcam is. */
export const CAPTURE_COPY = {
  lens: "Hold your ID up to the camera",
  eyebrow: "ID document · read by Amazon Textract",
  title: "Fit the page inside the frame",
  lead: "Hold it flat and still, about a hand's length from the camera. Avoid glare.",
  take: "Take photo",
  checkTitle: "Is everything sharp?",
  checkLead: "The two lines of letters and numbers at the bottom must be readable.",
  use: "Use this photo",
  retake: "Retake",
  uploadInstead: "Upload a photo instead",
  cancel: "Cancel",
  starting: "Starting the camera…",
  cameraError:
    "We couldn't open the camera. Close any other app that is using it, or upload a photo instead.",
  guidePassport: "The photo page",
  guideCard: "The front of the card",
};

export const SELFIE_COPY = {
  title: "Selfie video",
  lead: "Look up at the camera and follow the prompts.",
  verifying: "Checking…",
  done: "Live person confirmed",
  doneNext: "Next: check your details and submit.",
  finished: "Face check finished",
  stopped: "Face check stopped",
};

export const REVIEW_COPY = {
  title: "Check and submit",
  lead: "Make sure everything is right. You can change anything before you submit.",
  aboutYou: "About you",
  name: "Name",
  dob: "Date of birth",
  nationality: "Nationality",
  address: "Home address",
  idDocument: "ID document",
  selfie: "Selfie video",
  selfieDone: "Live check complete",
  account: "Account",
  edit: "Edit",
  retake: "Retake",
  redo: "Redo",
  declaration:
    "By submitting, you confirm that these details are true and that you're the person in the " +
    "photos.",
  submit: "Submit application",
};

// ------------------------------------------------------------------------- after submitting

/** Before an account exists, the application's status is what the applicant is told. */
export const STATUS_COPY: Record<ApplicantStatus, Copy> = {
  SUBMITTED: {
    title: "Application received",
    body: "We've received your application and are starting our checks.",
  },
  CHECKING: {
    title: "Verifying your identity",
    body: "We're checking your identity and your document. This usually takes under 90 seconds.",
  },
  MORE_INFORMATION_NEEDED: {
    title: "One more thing",
    body: "We need one thing from you before we can continue.",
  },
  IN_REVIEW: {
    title: "We're reviewing your application",
    body:
      "A member of our team is looking at your application, which can take a little longer. " +
      "You can keep this page open: it updates by itself as soon as there is news.",
  },
  OPEN_WITH_LIMITS: {
    title: "Your account is open",
    body: "Some features are limited for now while our standard checks finish.",
  },
  OPEN: {
    title: "Your account is open",
    body: "Your account is open for payments and transfers.",
  },
  ON_HOLD: {
    title: "Your account is temporarily unavailable",
    body: "You don't need to do anything right now, and we'll be in touch.",
  },
  DECLINED: {
    title: "We can't open an account for you",
    body:
      "We're unable to open an account with the details provided. You can start a new " +
      "application at any time.",
  },
};

/**
 * Once an account exists, the account is what the applicant is told about — never the case
 * behind it. A case back in review after opening is not news a bank shares with its customer.
 * The wording matches the guard's fixed templates (`api/notifications.py` `TEMPLATES`).
 */
export const ACCOUNT_COPY: Record<AccountState, Copy> = {
  OPEN_RESTRICTED: {
    title: "Your account is open",
    body:
      "Some features are limited while our standard checks finish. We'll let you know as soon " +
      "as that changes.",
  },
  OPEN_ENHANCED: {
    title: "Your account is open",
    body: "Payments and transfers are available. A limit on total deposits still applies.",
  },
  OPEN_STANDARD: {
    title: "Your account is fully open",
    body: "The earlier limits no longer apply.",
  },
  FROZEN: {
    title: "Your account is temporarily unavailable",
    body: "You don't need to do anything right now, and we'll be in touch.",
  },
};

export const PROGRESS_COPY = {
  slow:
    "This is taking longer than usual. Your application is safe with us, and this page keeps " +
    "checking for you.",
  keepOpen: "You can keep this page open. It updates by itself.",
  submitting: "Submitting your application…",
};

export const STEP_UP_COPY = {
  needed: "What we need",
  redoLiveness: "Redo the selfie video",
  newDocument: "Take a new photo of your ID",
  restart: "Start a new application",
  livenessHint:
    "We couldn't complete the selfie video. Let's take it again, facing the camera in good light.",
  documentHint:
    "We couldn't use the photo of your ID. Take a new one of the photo page, flat and in good light.",
};

export const HOME_COPY = {
  hello: "Hi",
  balance: "Available balance",
  statusLimited: "Limited for now",
  statusActive: "Active",
  statusFrozen: "Unavailable",
  limitsHeading: "Limited for now",
  depositsRemaining: "You can still deposit",
  actionsHeading: "Move money",
  refusedTitle: "Not available yet",
  dismiss: "Dismiss",
  insufficient: "Your balance doesn't cover this yet.",
  ceiling: "That would take your deposits over the limit for now.",
  activityHeading: "Recent activity",
  noActivity: "No activity yet.",
  completed: "Completed",
  notAvailable: "Not available",
  messagesHeading: "Messages",
  newBadge: "New",
  settingUp: "Your account is being set up.",
  unavailable: "We couldn't load your account just now. This page will try again.",
};

/** The one-tap actions on the account, and what each says once it has gone through. */
export const ACTION_COPY: Partial<Record<TransactionType, { label: string; done: string }>> = {
  DEPOSIT: { label: "Add money", done: "Added" },
  INTERNATIONAL_TRANSFER: { label: "Send abroad", done: "Sent abroad" },
  THIRD_PARTY_PAYMENT: { label: "Pay someone", done: "Paid" },
  CASH_WITHDRAWAL: { label: "Withdraw cash", done: "Withdrew" },
};

export const TRANSACTION_LABELS: Record<TransactionType, string> = {
  DEPOSIT: "Money added",
  DOMESTIC_TRANSFER: "Transfer to another bank",
  INTERNATIONAL_TRANSFER: "Sent abroad",
  THIRD_PARTY_PAYMENT: "Payment",
  CASH_WITHDRAWAL: "Cash withdrawal",
};

export const ERROR_COPY = {
  submit: "We could not submit your application. Please try again.",
  alreadySubmitted:
    "This application has already been submitted. Here is where it stands.",
  generic: "Something went wrong.",
  startOver: "Start over",
};
