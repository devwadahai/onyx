"use client";

import { useState, useMemo, useEffect } from "react";
import { useRouter } from "next/navigation";
import type { Route } from "next";
import { useSessionWatcher } from "@/lib/auth/hooks";
import { getExtensionContext } from "@/lib/extension/utils";
import { Modal } from "@opal/components";
import { Button, Text } from "@opal/components";
import {
  SvgLogOut,
  SvgCheckCircle,
  SvgXCircle,
  SvgSimpleLoader,
} from "@opal/icons";
import { SessionEndReason } from "@/lib/auth/types";
import { SvgGoogle } from "@opal/logos";
import { useCaptcha } from "@/lib/hooks/useCaptcha";
import { verifyCaptchaForOAuth } from "@/lib/auth/svc";
import {
  basicLogin,
  basicSignup,
  resendTwoFactorCode,
  verifyTwoFactorCode,
} from "@/lib/users/svc";
import { Formik } from "formik";
import * as Yup from "yup";
import { requestEmailVerification } from "@/lib/auth/svc";
import Link from "next/link";
import { useUser } from "@/providers/UserProvider";
import {
  validateInternalRedirect,
  passwordHasUppercase,
  passwordHasLowercase,
  passwordHasDigit,
  passwordHasSpecialChar,
  passwordMeetsLengthRequirements,
} from "@/lib/auth/utils";
import {
  AuthLayouts,
  Content,
  InputVertical,
  type AuthSubmitLabel,
  toast,
} from "@opal/layouts";
import InputTypeInField from "@/refresh-components/form/InputTypeInField";
import PasswordInputTypeInField from "@/refresh-components/form/PasswordInputTypeInField";
import { markdown } from "@opal/utils";
import { NEXT_PUBLIC_FORGOT_PASSWORD_ENABLED } from "@/lib/constants";

interface AuthenticationShellProps {
  children: React.ReactNode;
}

const SESSION_END_GENERIC_COPY =
  "Your session has expired. Please log in again to continue.";

const SESSION_END_COPY: Record<SessionEndReason, string> = {
  [SessionEndReason.EXPIRED]: SESSION_END_GENERIC_COPY,
  [SessionEndReason.TERMINATED]:
    "This session was signed out. Please log in again to continue.",
  [SessionEndReason.UNRECOGNIZED]:
    "You were signed out unexpectedly. Please log in again to continue. " +
    "If this keeps happening, contact your administrator.",
};

export function AuthenticationShell({ children }: AuthenticationShellProps) {
  const router = useRouter();
  const { sessionEnded, sessionEndReason } = useSessionWatcher();

  function handleLogin() {
    const { isExtension } = getExtensionContext();
    if (isExtension) {
      window.open(
        window.location.origin + "/auth/login",
        "_blank",
        "noopener,noreferrer"
      );
      return;
    }
    // Round-trip the current location through login (OAuth `next` / SAML
    // RelayState) so the post-login redirect lands back here.
    const returnTo = validateInternalRedirect(
      window.location.pathname + window.location.search + window.location.hash
    );
    router.push(
      returnTo
        ? (`/auth/login?next=${encodeURIComponent(returnTo)}` as Route)
        : "/auth/login"
    );
  }

  return (
    <>
      <div
        className={sessionEnded ? "pointer-events-none select-none" : undefined}
      >
        {children}
      </div>
      {sessionEnded && (
        <Modal open>
          <Modal.Content width="sm" height="sm">
            <Modal.Header icon={SvgLogOut} title="You Have Been Logged Out" />
            <Modal.Body>
              <Text font="main-ui-body" color="text-03">
                {sessionEndReason
                  ? SESSION_END_COPY[sessionEndReason]
                  : SESSION_END_GENERIC_COPY}
              </Text>
            </Modal.Body>
            <Modal.Footer>
              <Button onClick={handleLogin}>Log In</Button>
            </Modal.Footer>
          </Modal.Content>
        </Modal>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// SignInButton
//
// Renders the Google sign-in button on the login page.
//
// When reCAPTCHA is enabled for this deployment (NEXT_PUBLIC_RECAPTCHA_SITE_KEY
// set at build time), the Google OAuth click is intercepted to
// (1) fetch a reCAPTCHA v3 token for the "oauth" action, (2) POST it to
// /api/auth/captcha/oauth-verify which sets a signed HttpOnly cookie on the
// response, and (3) then navigate to the authorize URL. The cookie is sent
// automatically on the subsequent Google redirect back to our callback,
// where the backend middleware verifies it.
//
// IMPORTANT: This component is rendered as part of the /auth/login page, which
// is used in healthcheck and monitoring flows that issue headless (non-browser)
// requests (e.g. `curl`). During server-side rendering of those requests,
// browser-only globals like `window`, `document`, `navigator`, etc. are NOT
// available. Even though this file is marked "use client", Next.js still
// executes the component body on the server during SSR — only hooks like
// `useEffect` are skipped.
//
// Do NOT reference `window` or other browser APIs in the render path of this
// component. If you need browser globals, gate them behind `useEffect` or
// `typeof window !== "undefined"` checks inside callbacks/effects — but be
// aware that Turbopack may optimise away bare `typeof window` guards in the
// SSR bundle, so prefer `useEffect` for safety.
// ---------------------------------------------------------------------------

interface SignInButtonProps {
  authorizeUrl: string;
}

export function SignInButton({ authorizeUrl }: SignInButtonProps) {
  const { getCaptchaToken, isCaptchaEnabled } = useCaptcha();
  const [isVerifying, setIsVerifying] = useState(false);

  async function handleClick(e: React.MouseEvent) {
    e.preventDefault();
    if (isVerifying) return;
    setIsVerifying(true);
    // Stays true on the success branch so the button remains disabled until
    // the browser actually begins unloading for the OAuth redirect — prevents
    // a double-click window between `window.location.href = ...` and unload.
    let navigating = false;
    try {
      const token = await getCaptchaToken("oauth");
      if (!token) {
        toast.error("grecaptcha.execute returned no token");
        return;
      }
      await verifyCaptchaForOAuth(token);
      navigating = true;
      window.location.href = authorizeUrl;
    } catch (exc) {
      toast.error(exc instanceof Error ? exc.message : String(exc));
    } finally {
      if (!navigating) setIsVerifying(false);
    }
  }

  // The Google OAuth callback is gated by CaptchaCookieMiddleware on the
  // backend, so the click is intercepted whenever reCAPTCHA is enabled.
  const intercepted = isCaptchaEnabled;

  return (
    <Button
      prominence="secondary"
      width="full"
      icon={SvgGoogle}
      href={intercepted ? undefined : authorizeUrl}
      onClick={intercepted ? handleClick : undefined}
      disabled={isVerifying}
    >
      Continue with Google
    </Button>
  );
}

// ---------------------------------------------------------------------------
// PasswordRequirements
// ---------------------------------------------------------------------------

interface PasswordRequirementsProps {
  password: string;
}

export function PasswordRequirements({ password }: PasswordRequirementsProps) {
  const { authTypeMetadata } = useUser();

  if (!authTypeMetadata) return null;

  const {
    passwordMinLength,
    passwordMaxLength,
    passwordRequireUppercase,
    passwordRequireLowercase,
    passwordRequireDigit,
    passwordRequireSpecialChar,
  } = authTypeMetadata;

  const rules = (
    [
      {
        label: `${passwordMinLength}–${passwordMaxLength} characters`,
        met: passwordMeetsLengthRequirements(
          password,
          passwordMinLength,
          passwordMaxLength
        ),
      },
      passwordRequireUppercase && {
        label: "Contains uppercase letter.",
        met: passwordHasUppercase(password),
      },
      passwordRequireLowercase && {
        label: "Contains lowercase letter.",
        met: passwordHasLowercase(password),
      },
      passwordRequireDigit && {
        label: "Contains number.",
        met: passwordHasDigit(password),
      },
      passwordRequireSpecialChar && {
        label: "Contains special character.",
        met: passwordHasSpecialChar(password),
      },
    ] as const
  ).filter((r): r is { label: string; met: boolean } => Boolean(r));

  return (
    <div className="flex flex-col gap-1">
      {rules.map((rule) => (
        <Content
          key={rule.label}
          sizePreset="secondary"
          variant="body"
          icon={rule.met ? SvgCheckCircle : SvgXCircle}
          color={rule.met ? "success" : "muted"}
          title={rule.label}
        />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// EmailPasswordForm
// ---------------------------------------------------------------------------

interface FormValues {
  email: string;
  password: string;
}

/** Shared by both the direct-login success path and the post-2FA-verify
 * success path -- same destination either way. */
function redirectAfterLogin(
  nextUrl: string | null | undefined,
  isSignup: boolean,
  isJoin: boolean
) {
  const validatedNextUrl = validateInternalRedirect(nextUrl);
  window.location.href =
    validatedNextUrl ?? `/app${isSignup && !isJoin ? "?new_team=true" : ""}`;
}

interface TwoFactorState {
  preAuthToken: string;
}

export interface EmailPasswordFormProps {
  shouldVerify?: boolean;
  referralSource?: string;
  nextUrl?: string | null;
  defaultEmail?: string | null;
  label: AuthSubmitLabel;
}

export function EmailPasswordForm({
  shouldVerify,
  referralSource,
  nextUrl,
  defaultEmail,
  label,
}: EmailPasswordFormProps) {
  const isSignup = label !== "submit";
  const isJoin = label === "join";

  const { user, authTypeMetadata } = useUser();
  const { getCaptchaToken } = useCaptcha();
  const [twoFactor, setTwoFactor] = useState<TwoFactorState | null>(null);

  const validationSchema = useMemo(() => {
    let passwordSchema = Yup.string();

    if (isSignup) {
      const minLength = authTypeMetadata?.passwordMinLength ?? 0;
      const maxLength = authTypeMetadata?.passwordMaxLength ?? Infinity;

      passwordSchema = passwordSchema.test(
        "length",
        `Password must be between ${minLength}–${maxLength} characters`,
        (v) => passwordMeetsLengthRequirements(v ?? "", minLength, maxLength)
      );

      if (authTypeMetadata?.passwordRequireUppercase)
        passwordSchema = passwordSchema.test(
          "uppercase",
          "Password must contain at least one uppercase letter",
          (v) => passwordHasUppercase(v ?? "")
        );
      if (authTypeMetadata?.passwordRequireLowercase)
        passwordSchema = passwordSchema.test(
          "lowercase",
          "Password must contain at least one lowercase letter",
          (v) => passwordHasLowercase(v ?? "")
        );
      if (authTypeMetadata?.passwordRequireDigit)
        passwordSchema = passwordSchema.test(
          "digit",
          "Password must contain at least one number",
          (v) => passwordHasDigit(v ?? "")
        );
      if (authTypeMetadata?.passwordRequireSpecialChar)
        passwordSchema = passwordSchema.test(
          "special-char",
          "Password must contain at least one special character",
          (v) => passwordHasSpecialChar(v ?? "")
        );
    }

    return Yup.object().shape({
      email: Yup.string()
        .email()
        .required()
        .transform((value: string) => value.toLowerCase()),
      password: passwordSchema.required(),
    });
  }, [isSignup, authTypeMetadata]);

  const initialValues: FormValues = {
    email: defaultEmail?.toLowerCase() ?? "",
    password: "",
  };

  async function handleSubmit(values: FormValues) {
    const email = values.email.toLowerCase();

    if (isSignup) {
      const captchaToken = await getCaptchaToken("signup");
      const response = await basicSignup(
        email,
        values.password,
        referralSource,
        captchaToken
      );

      if (!response.ok) {
        const errorBody: any = await response.json().catch(() => ({}));
        const errorDetail = errorBody.detail;
        let errorMsg = "Unknown error";
        if (response.status === 429) {
          errorMsg = "Too many requests. Please try again later.";
        } else if (errorDetail === "REGISTER_USER_ALREADY_EXISTS") {
          errorMsg = "An account already exists with the specified email.";
        } else if (typeof errorDetail === "string" && errorDetail) {
          errorMsg = errorDetail;
        }
        toast.error(errorMsg);
        return;
      }

      // On verification-required deployments the server blocks login until the
      // email is confirmed, so we must NOT call basicLogin first — it would
      // fail and leave the user stranded even though the account was created.
      if (shouldVerify) {
        try {
          await requestEmailVerification(email);
        } catch (e) {
          // Best-effort: the account already exists, so redirect regardless.
          console.warn("requestEmailVerification failed:", e);
        }
        window.location.href = "/auth/waiting-on-verification";
        return;
      }
    }

    const loginCaptchaToken = await getCaptchaToken("login");
    const loginResponse = await basicLogin(
      email,
      values.password,
      loginCaptchaToken
    );

    if (loginResponse.ok) {
      // A real session login (the common case) sets its cookie via a bare
      // 204 with no body -- fastapi_users' CookieTransport default. Only
      // the 2FA-pending branch (added in two_factor/api.py) returns a JSON
      // body on this same 200-ish success path, so a body is what
      // distinguishes "logged in" from "enter your code".
      if (loginResponse.status !== 204) {
        const body: any = await loginResponse.json().catch(() => null);
        if (body?.requires_2fa && body?.pre_auth_token) {
          setTwoFactor({ preAuthToken: body.pre_auth_token });
          return;
        }
      }
      redirectAfterLogin(nextUrl, isSignup, isJoin);
    } else {
      const errorBody: any = await loginResponse.json().catch(() => ({}));
      const errorDetail = errorBody.detail;
      let errorMsg = "Unknown error";
      if (loginResponse.status === 429) {
        errorMsg = "Too many requests. Please try again later.";
      } else if (errorDetail === "LOGIN_BAD_CREDENTIALS") {
        errorMsg = "Invalid email or password";
      } else if (errorDetail === "NO_WEB_LOGIN_AND_HAS_NO_PASSWORD") {
        errorMsg = "Create an account to set a password";
      } else if (errorDetail === "PASSWORD_LOGIN_DISABLED") {
        errorMsg =
          "Password login is turned off. Sign in with your identity provider.";
      } else if (typeof errorDetail === "string") {
        errorMsg = errorDetail;
      }
      toast.error(errorMsg);
    }
  }

  if (twoFactor) {
    return (
      <TwoFactorCodeForm
        preAuthToken={twoFactor.preAuthToken}
        nextUrl={nextUrl}
        isSignup={isSignup}
        isJoin={isJoin}
        onGiveUp={() => setTwoFactor(null)}
      />
    );
  }

  return (
    <Formik
      initialValues={initialValues}
      enableReinitialize
      validateOnChange={true}
      validateOnBlur={true}
      validationSchema={validationSchema}
      onSubmit={handleSubmit}
    >
      {({ isSubmitting, isValid, dirty, values, errors }) => {
        return (
          <AuthLayouts.FormBody>
            <AuthLayouts.Fields>
              <InputVertical title="Email Address" withLabel="email">
                <InputTypeInField
                  name="email"
                  placeholder="email@yourcompany.com"
                  data-testid="email"
                  autoComplete="username"
                />
              </InputVertical>

              <div className="flex flex-col gap-1">
                <InputVertical
                  title="Password"
                  withLabel="password"
                  topRight={
                    NEXT_PUBLIC_FORGOT_PASSWORD_ENABLED &&
                    !isSignup &&
                    !errors.email &&
                    !!values.email
                      ? markdown(
                          `[Forgot password?](/auth/forgot-password?email=${encodeURIComponent(values.email)})`
                        )
                      : undefined
                  }
                  subDescription={
                    isSignup ? "Password requirements:" : undefined
                  }
                >
                  <PasswordInputTypeInField
                    name="password"
                    placeholder="Password"
                    mask="native"
                    data-testid="password"
                    autoComplete={
                      isSignup ? "new-password" : "current-password"
                    }
                  />
                </InputVertical>
                {isSignup && (
                  <PasswordRequirements password={values.password} />
                )}
              </div>
            </AuthLayouts.Fields>

            <AuthLayouts.Submit
              label={label}
              isSubmitting={isSubmitting}
              isValid={isValid && (!isSignup || Boolean(authTypeMetadata))}
              dirty={dirty}
            />

            {user?.is_anonymous_user && (
              <Link
                href="/app"
                className="text-xs text-action-selection-05 cursor-pointer text-center w-full font-medium mx-auto"
              >
                <span className="hover:border-b hover:border-dotted hover:border-action-selection-05">
                  or continue as guest
                </span>
              </Link>
            )}
          </AuthLayouts.FormBody>
        );
      }}
    </Formik>
  );
}

// ---------------------------------------------------------------------------
// TwoFactorCodeForm — SMS 2FA "enter your code" step, shown after a
// password login returns a pre-auth token instead of a real session.
// ---------------------------------------------------------------------------

const RESEND_COOLDOWN_FALLBACK_SECONDS = 30; // mirrors OTP_RESEND_MIN_INTERVAL_SECONDS

interface TwoFactorCodeFormProps {
  preAuthToken: string;
  nextUrl?: string | null;
  isSignup: boolean;
  isJoin: boolean;
  onGiveUp: () => void;
}

interface TwoFactorFormValues {
  code: string;
}

const twoFactorValidationSchema = Yup.object().shape({
  code: Yup.string()
    .matches(/^\d{6}$/, "Enter the 6-digit code")
    .required("Enter the 6-digit code"),
});

function TwoFactorCodeForm({
  preAuthToken,
  nextUrl,
  isSignup,
  isJoin,
  onGiveUp,
}: TwoFactorCodeFormProps) {
  const [isResending, setIsResending] = useState(false);
  const [resendCooldown, setResendCooldown] = useState(0);

  useEffect(() => {
    if (resendCooldown <= 0) return;
    const interval = setInterval(() => {
      setResendCooldown((seconds) => Math.max(0, seconds - 1));
    }, 1000);
    return () => clearInterval(interval);
  }, [resendCooldown]);

  async function handleSubmit(
    values: TwoFactorFormValues,
    { setSubmitting }: { setSubmitting: (submitting: boolean) => void }
  ) {
    const response = await verifyTwoFactorCode(preAuthToken, values.code);

    if (response.ok) {
      redirectAfterLogin(nextUrl, isSignup, isJoin);
      return;
    }

    const errorBody: any = await response.json().catch(() => ({}));
    const errorDetail = errorBody.detail;

    if (response.status === 401) {
      // The pre-auth token itself is dead (expired/invalid) -- no code will
      // help, so send them back to log in again rather than retry in place.
      toast.error(
        typeof errorDetail === "string"
          ? errorDetail
          : "Your verification session expired. Please log in again."
      );
      onGiveUp();
      return;
    }

    let errorMsg = "Invalid code";
    if (errorDetail === "expired") {
      errorMsg = "That code expired. Request a new one below.";
    } else if (errorDetail === "too_many_attempts") {
      errorMsg = "Too many incorrect attempts. Request a new one below.";
    } else if (errorDetail === "no_code") {
      errorMsg = "No active code found. Request a new one below.";
    } else if (errorDetail === "mismatch") {
      errorMsg = "That code doesn't look right. Try again.";
    } else if (typeof errorDetail === "string" && errorDetail) {
      errorMsg = errorDetail;
    }
    toast.error(errorMsg);
    setSubmitting(false);
  }

  async function handleResend() {
    setIsResending(true);
    try {
      const response = await resendTwoFactorCode(preAuthToken);
      const body: any = await response.json().catch(() => ({}));

      if (response.status === 401) {
        toast.error("Your verification session expired. Please log in again.");
        onGiveUp();
        return;
      }

      if (body?.sent) {
        toast.success("Sent! Check your phone for a new code.");
        setResendCooldown(RESEND_COOLDOWN_FALLBACK_SECONDS);
      } else if (body?.reason === "rate_limited") {
        setResendCooldown(
          body?.retry_after_seconds ?? RESEND_COOLDOWN_FALLBACK_SECONDS
        );
      } else {
        toast.error(
          "Couldn't send a new code right now. Please try again shortly."
        );
      }
    } finally {
      setIsResending(false);
    }
  }

  return (
    <Formik
      initialValues={{ code: "" }}
      validateOnChange={true}
      validateOnBlur={true}
      validationSchema={twoFactorValidationSchema}
      onSubmit={handleSubmit}
    >
      {({ isSubmitting, isValid, dirty }) => (
        <AuthLayouts.FormBody>
          <AuthLayouts.Message
            title="Check your phone"
            description="We texted a 6-digit verification code to the phone number on your account."
          />
          <AuthLayouts.Fields>
            <InputVertical title="Verification Code" withLabel="code">
              <InputTypeInField
                name="code"
                placeholder="123456"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                data-testid="two-factor-code"
              />
            </InputVertical>
          </AuthLayouts.Fields>

          <Button
            type="submit"
            width="full"
            disabled={isSubmitting || !isValid || !dirty}
            icon={isSubmitting ? SvgSimpleLoader : undefined}
          >
            Verify
          </Button>

          <div className="flex flex-row items-center justify-between w-full">
            <span
              onClick={onGiveUp}
              className="text-xs text-text-03 cursor-pointer hover:underline"
            >
              Back to log in
            </span>
            <span
              onClick={
                isResending || resendCooldown > 0 ? undefined : handleResend
              }
              className={
                isResending || resendCooldown > 0
                  ? "text-xs text-text-03 cursor-not-allowed"
                  : "text-xs text-action-selection-05 cursor-pointer hover:underline"
              }
            >
              {resendCooldown > 0
                ? `Resend code (${resendCooldown}s)`
                : "Resend code"}
            </span>
          </div>
        </AuthLayouts.FormBody>
      )}
    </Formik>
  );
}
