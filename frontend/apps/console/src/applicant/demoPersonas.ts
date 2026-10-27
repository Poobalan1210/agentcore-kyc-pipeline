import { DeclaredIdentity } from "./types";

/**
 * Demo personas: what the presenter picks from the console's New application menu, so the
 * customer's app opens with the details filled in. Each matches one generated document in
 * `demo-assets/generated/`. The customer's screen never shows the list: a bank app has no
 * persona picker.
 *
 * This duplicates `demo-assets/gen_documents.py`'s `DEMO_IDENTITIES`, because the two live in
 * different languages and deployment units. The duplication is held in check by a test rather
 * than by care: `demo-assets/tests/test_gen_documents.py` parses this file and fails if any
 * persona's fields differ from its document — a difference that would otherwise surface on stage
 * as a declared-versus-document mismatch and send the case to a reviewer. Keep each persona's
 * fields one per line, as below, so that test can read them.
 *
 * Every field stays editable on purpose: changing the date of birth to a wrong value is how the
 * mismatch-detection path gets demonstrated.
 *
 * The U.S. personas' `taxId`s are from 987-65-4320 to 987-65-4329, the range SSA reserves for
 * advertisements and never issues, so no persona carries a real person's number. They read as
 * ITINs to the format check (an SSN never starts with 9), which is why every screen says "SSN or
 * ITIN" rather than guessing which.
 */
export interface DemoPersona extends DeclaredIdentity {
  id: string;
  jurisdiction: string;
  /** A few words for the presenter's menu. */
  tag: string;
  /** What this persona shows on stage, as `gen_documents.py` states it. */
  scenario: string;
  /** The generated document to upload for this persona. */
  documentFile: string;
}

export const DEMO_PERSONAS: DemoPersona[] = [
  {
    id: "DEMO-US-01",
    jurisdiction: "us",
    tag: "Clean path",
    scenario: "Clean path: a provisional account in under 90 seconds, limits lifted later",
    documentFile: "demo-us-01-passport.jpg",
    givenNames: "TAYLOR JORDAN",
    surname: "REEVES",
    dateOfBirth: "1991-05-14",
    nationality: "USA",
    address: "482 Kestrel Way, Austin, TX 78701, USA",
    taxId: "987-65-4320",
  },
  {
    id: "DEMO-US-02",
    jurisdiction: "us",
    tag: "Possible match",
    scenario: "Possible match: SYN-0106 'Avery Hale' is listed with no date of birth, so a person decides; approving clears the alert for good",
    documentFile: "demo-us-02-passport.jpg",
    givenNames: "AVERY QUINN",
    surname: "HALE",
    dateOfBirth: "1990-03-22",
    nationality: "USA",
    address: "1150 Larkspur Avenue, Denver, CO 80203, USA",
    taxId: "987-65-4321",
  },
  {
    id: "DEMO-US-03",
    jurisdiction: "us",
    tag: "Act 3",
    scenario: "Act 3: clean at onboarding; the list update SYN-0901 names her, and the re-screen of her open account freezes it",
    documentFile: "demo-us-03-passport.jpg",
    givenNames: "NADIA ROSE",
    surname: "KORVAL",
    dateOfBirth: "1987-02-11",
    nationality: "USA",
    address: "77 Harbor View Drive, Seattle, WA 98101, USA",
    taxId: "987-65-4322",
  },
  {
    id: "DEMO-US-04",
    jurisdiction: "us",
    tag: "Namesake",
    scenario: "Namesake: the exact name of SYN-0001, born 26 years later; excluded on date of birth, with the reason recorded, and approved",
    documentFile: "demo-us-04-passport.jpg",
    givenNames: "DMITRI",
    surname: "VOLKOV",
    dateOfBirth: "1994-10-05",
    nationality: "USA",
    address: "2209 Birchwood Lane, Chicago, IL 60614, USA",
    taxId: "987-65-4323",
  },
  {
    id: "DEMO-UK-01",
    jurisdiction: "uk",
    tag: "Outside the US",
    scenario: "Outside the US: goes to a reviewer, because this build decides only US cases automatically",
    documentFile: "demo-uk-01-drivers_license.jpg",
    givenNames: "ALISTAIR",
    surname: "OSEI-BRIGHT",
    dateOfBirth: "1988-11-02",
    nationality: "GBR",
    address: "14 Harrow Close, Manchester, M1 4BT, United Kingdom",
  },
  {
    id: "DEMO-AU-01",
    jurisdiction: "au",
    tag: "Outside the US",
    scenario: "Outside the US: goes to a reviewer, because this build decides only US cases automatically",
    documentFile: "demo-au-01-national_id.jpg",
    givenNames: "MORGAN KIRI",
    surname: "WHITLAM",
    dateOfBirth: "1995-02-27",
    nationality: "AUS",
    address: "27 Banksia Street, Fremantle, WA 6160, Australia",
  },
  {
    id: "DEMO-JP-01",
    jurisdiction: "jp",
    tag: "Outside the US",
    scenario: "Outside the US: goes to a reviewer, because this build decides only US cases automatically",
    documentFile: "demo-jp-01-passport.jpg",
    givenNames: "HARUTO",
    surname: "ICHIKAWA",
    dateOfBirth: "1993-08-19",
    nationality: "JPN",
    address: "3-2-1 Marunouchi, Chiyoda-ku, Tokyo 100-0005, Japan",
  },
];

export const BLANK_IDENTITY: DeclaredIdentity = {
  givenNames: "",
  surname: "",
  dateOfBirth: "",
  nationality: "",
  address: "",
};

export function personaById(id: string | null | undefined): DemoPersona | undefined {
  return id ? DEMO_PERSONAS.find((p) => p.id === id) : undefined;
}

/** Just the declared fields, so a persona's demo metadata never reaches the API. */
export function identityOf(persona: DemoPersona | undefined): DeclaredIdentity {
  if (!persona) return { ...BLANK_IDENTITY };
  const { givenNames, surname, dateOfBirth, nationality, address, taxId } = persona;
  return { givenNames, surname, dateOfBirth, nationality, address, taxId };
}

/** The API's document type for a persona, read from its generated file's name
 * (`demo-uk-01-drivers_license.jpg` -> `DRIVERS_LICENSE`). */
export function documentTypeOf(persona: DemoPersona): string {
  if (persona.documentFile.includes("drivers_license")) return "DRIVERS_LICENSE";
  if (persona.documentFile.includes("national_id")) return "NATIONAL_ID";
  return "PASSPORT";
}
