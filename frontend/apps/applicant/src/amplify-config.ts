/**
 * Amplify Auth configuration — task 14.5, F-42.
 *
 * Amplify will not render `FaceLivenessDetector` without a configured Auth backend (F-42), even
 * though this app never calls Amplify Auth's own sign-up/sign-in UI — Cognito here is used for
 * two distinct things: the user pool for the app's own login (via the Public API's JWT
 * authorizer), and the identity pool purely so `FaceLivenessDetector` can sign its direct
 * request to the Rekognition streaming service (R33.7).
 *
 * All four values come from `KycAuth`'s CDK outputs (`auth_stack.py`) — this file has no
 * hardcoded fallback, so a missing env var fails loudly at build/dev time rather than silently
 * pointing at nothing.
 */

import { Amplify } from "aws-amplify";

function required(name: string): string {
  const value = import.meta.env[name];
  if (!value) {
    throw new Error(`${name} is not set — see frontend/apps/applicant/.env.local.example`);
  }
  return value as string;
}

export function configureAmplify(): void {
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: required("VITE_USER_POOL_ID"),
        userPoolClientId: required("VITE_USER_POOL_CLIENT_ID"),
        identityPoolId: required("VITE_IDENTITY_POOL_ID"),
      },
    },
  });
}
