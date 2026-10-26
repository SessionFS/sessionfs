import { Component, type ErrorInfo, type ReactNode } from 'react';

/**
 * Contains a render error in one helper answer (the text is model-written and
 * stored, so a bad answer would otherwise take the drawer down every time the
 * conversation is reopened). Tries again when the answer text changes.
 */
export default class AnswerBoundary extends Component<
  { resetKey: string; children: ReactNode },
  { failed: boolean; key: string }
> {
  state = { failed: false, key: this.props.resetKey };

  static getDerivedStateFromError(): Partial<{ failed: boolean }> {
    return { failed: true };
  }

  static getDerivedStateFromProps(props: { resetKey: string }, state: { failed: boolean; key: string }) {
    return props.resetKey !== state.key ? { failed: false, key: props.resetKey } : null;
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Helper answer failed to render', error, info.componentStack);
  }

  render() {
    if (this.state.failed) {
      return <p className="text-sm text-text-tertiary">This answer couldn’t be displayed.</p>;
    }
    return this.props.children;
  }
}
