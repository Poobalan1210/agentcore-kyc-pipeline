import { Amplify } from "aws-amplify";

function required(name: string): string {
  const value = import.meta.env[name];
  if (!value) {
    throw new Error(`${name} is not set — see frontend/apps/reviewer/.env.local.example`);
  }
  return value as string;
}

/** Reviewer console needs only the user pool — it never calls Rekognition, so no identity pool
 * credential is required here the way the applicant app needs one for FaceLivenessDetector. */
export function configureAmplify(): void {
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: required("VITE_USER_POOL_ID"),
        userPoolClientId: required("VITE_USER_POOL_CLIENT_ID"),
      },
    },
  });
}
