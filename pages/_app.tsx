import type { AppProps } from "next/app";

export default function AurexApp({ Component, pageProps }: AppProps) {
  return <Component {...pageProps} />;
}
