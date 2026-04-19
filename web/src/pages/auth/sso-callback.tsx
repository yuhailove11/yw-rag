import { useOAuthCallback } from '@/hooks/auth-hooks';

const SsoCallback = () => {
  useOAuthCallback();

  return (
    <div className="flex min-h-screen items-center justify-center bg-background text-text-primary">
      <div className="rounded-2xl border border-border bg-bg-component px-8 py-10 text-center shadow-sm">
        <div className="mb-3 text-lg font-semibold">正在完成统一登录</div>
        <div className="text-sm text-text-secondary">请稍候，系统正在校验票据并跳转。</div>
      </div>
    </div>
  );
};

export default SsoCallback;
