import { DeclaredIdentity } from "./types";

/**
 * Pre-fill values for the details form, one per jurisdiction, matching the corresponding
 * generated document in `demo-assets/generated/`.
 *
 * This duplicates `demo-assets/gen_documents.py`'s `DEMO_IDENTITIES`. The duplication is
 * deliberate rather than overlooked: the two live in different languages and different
 * deployment units (a local asset script vs. a browser bundle), and wiring a build step to share
 * them would add more machinery than the four records are worth. `demoPersonas.test.ts` is not
 * possible here, so the risk is contained instead by the mismatch detection itself — if these
 * ever drift from the documents, the pipeline reports a name or DOB mismatch, which is a visible
 * failure rather than a silent one.
 *
 * The form is pre-filled so the clean path needs no typing on stage. Editing any field to a wrong
 * value is how the mismatch-detection path gets demonstrated, so nothing here is read-only.
 */
export const DEMO_PERSONAS: Record<string, DeclaredIdentity> = {
  us: {
    givenNames: "TAYLOR JORDAN",
    surname: "REEVES",
    dateOfBirth: "1991-05-14",
    nationality: "USA",
    address: "482 Kestrel Way, Austin, TX 78701, USA",
  },
  uk: {
    givenNames: "ALISTAIR",
    surname: "OSEI-BRIGHT",
    dateOfBirth: "1988-11-02",
    nationality: "GBR",
    address: "14 Harrow Close, Manchester, M1 4BT, United Kingdom",
  },
  au: {
    givenNames: "MORGAN KIRI",
    surname: "WHITLAM",
    dateOfBirth: "1995-02-27",
    nationality: "AUS",
    address: "27 Banksia Street, Fremantle, WA 6160, Australia",
  },
  jp: {
    givenNames: "HARUTO",
    surname: "ICHIKAWA",
    dateOfBirth: "1993-08-19",
    nationality: "JPN",
    address: "3-2-1 Marunouchi, Chiyoda-ku, Tokyo 100-0005, Japan",
  },
};

export function personaFor(jurisdiction: string): DeclaredIdentity {
  return (
    DEMO_PERSONAS[jurisdiction] ?? {
      givenNames: "",
      surname: "",
      dateOfBirth: "",
      nationality: "",
      address: "",
    }
  );
}
