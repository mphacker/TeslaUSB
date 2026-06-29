import { ApiError } from "../api/client";

export interface DeleteFailure {
  message: string;
  retryable: boolean;
  softGone: boolean;
}

export function classifyDeleteFailure(err: unknown): DeleteFailure {
  if (err instanceof ApiError) {
    if (err.status === 0 || err.code === "network") {
      return {
        message: "Couldn't reach the device. Check the connection and retry.",
        retryable: true,
        softGone: false,
      };
    }
    if (err.status === 409) {
      if (err.code === "not_present") {
        return {
          message: "This clip is already gone from the car — removing it.",
          retryable: false,
          softGone: true,
        };
      }
      return {
        message: `${err.message} You can retry in a moment.`,
        retryable: true,
        softGone: false,
      };
    }
    if (err.status === 404) {
      return {
        message: "That clip no longer exists — removing it.",
        retryable: false,
        softGone: true,
      };
    }
    if (err.status === 503) {
      return {
        message: "The device is unreachable right now. Try again once it's back.",
        retryable: true,
        softGone: false,
      };
    }
    if (err.status === 400 || err.status === 422) {
      return { message: err.message, retryable: false, softGone: false };
    }
    if (err.status === 502) {
      return {
        message: `The delete couldn't be completed on the car: ${err.message}`,
        retryable: false,
        softGone: false,
      };
    }
    if (err.status === 500) {
      return {
        message: `The device reported a fault during delete: ${err.message}`,
        retryable: false,
        softGone: false,
      };
    }
    if (err.status === 501) {
      return {
        message: "Only car-side delete is available.",
        retryable: false,
        softGone: false,
      };
    }
    return { message: err.message, retryable: false, softGone: false };
  }
  return {
    message: (err as Error).message || "Unexpected error.",
    retryable: true,
    softGone: false,
  };
}
