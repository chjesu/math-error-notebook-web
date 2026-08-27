window.__ModuleLoader__.load({
  id: "@lizhaolin/dsh-math-notebook-ui",
  factory: (require) => {
    const module = {exports: {}};
    const exports = module.exports;
    const {jsx} = require("react/jsx-runtime");
    const productOrigin = "http://127.0.0.1:8000";
    const productWorkspaceTitle = "错题会话";

    function installStudentSurface(ctx) {
      ctx.effect(() => {
        const style = document.createElement("style");
        style.dataset.pluginCss = "@lizhaolin/dsh-math-notebook-ui/student-surface";
        style.textContent = `
          button[aria-label="选择工作区"],
          button[aria-label="Choose workspace"],
          button[aria-label="添加工作区"],
          button[aria-label="Add workspace"] {
            display: none !important;
          }
        `;
        document.head.appendChild(style);
        return () => style.remove();
      }, "math-notebook: student surface");
    }

    function openProductWorkspace(ctx) {
      ctx.effect(() => {
        let connecting = false;
        const open = () => {
          const snapshot = ctx.workspaces.list.getSnapshot();
          const workspace = snapshot.items.find((item) => item.title === productWorkspaceTitle);
          if (connecting || !snapshot.baselinesReady || workspace === undefined) return;
          if (ctx.sessions.list.getSnapshot().current !== undefined) return;
          connecting = true;
          ctx.workspaces.connectWorkspace(workspace.workspaceId).then((sessionId) => {
            if (ctx.sessions.list.getSnapshot().current === undefined) ctx.sessions.open(sessionId);
          }).catch((reason) => {
            connecting = false;
            console.warn("math notebook workspace connection failed:", reason);
          });
        };
        const unsubscribe = ctx.workspaces.list.subscribe(open);
        open();
        return unsubscribe;
      }, "math-notebook: fixed workspace");
    }

    function BrandMark({size, className}) {
      return jsx("img", {
        src: `${productOrigin}/assets/branding/logo-symbol-color-64-v1.png`,
        width: size,
        height: size,
        className,
        alt: ""
      });
    }

    function BrandName() {
      return jsx("span", {
        style: {fontSize: "14px", fontWeight: 700, whiteSpace: "nowrap"},
        children: "李兆霖数学错题本"
      });
    }

    function NotebookHome({wide}) {
      return jsx("a", {
        href: `${productOrigin}/errors`,
        target: "_top",
        title: "打开错题本",
        style: {
          display: "flex",
          minHeight: "36px",
          alignItems: "center",
          justifyContent: wide ? "flex-start" : "center",
          gap: "8px",
          padding: wide ? "0 10px" : "0",
          borderRadius: "8px",
          color: "inherit",
          textDecoration: "none"
        },
        children: wide ? "错题本与复习" : "📘"
      });
    }

    const inject = ["slots", "sessions", "workspaces"];
    function apply(ctx) {
      installStudentSurface(ctx);
      openProductWorkspace(ctx);
      ctx.slots.inject("sidebar.brand.mark", () =>
        ctx.slots.inject("sidebar.brand.name", () =>
          ctx.slots.inject("conversation.hero.brand.mark", () =>
            ctx.slots.inject("sidebar.footer.action", function* () {
              yield ctx.slots.register({name: "sidebar.brand.mark"}, BrandMark);
              yield ctx.slots.register({name: "sidebar.brand.name"}, BrandName);
              yield ctx.slots.register({name: "conversation.hero.brand.mark"}, BrandMark);
              yield ctx.slots.register({
                name: "sidebar.footer.action",
                id: "math-notebook-home",
                order: 100,
                label: "错题本与复习"
              }, NotebookHome);
            }))));
    }

    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  }
});
