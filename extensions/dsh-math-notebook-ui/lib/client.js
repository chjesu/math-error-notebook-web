window.__ModuleLoader__.load({
  id: "@lizhaolin/dsh-math-notebook-ui",
  factory: (require) => {
    const module = {exports: {}};
    const exports = module.exports;
    const {jsx} = require("react/jsx-runtime");
    const productOrigin = "http://127.0.0.1:8000";
    const productWorkspaceTitle = "错题会话";
    const navigationItems = [
      {path: "/", label: "工作台", icon: "workbench"},
      {path: "/errors", label: "错题本", icon: "errors"},
      {path: "/reviews", label: "今日复习", icon: "reviews"},
      {path: "/practice", label: "练习 PDF", icon: "practice"},
      {path: "/progress", label: "学习进度", icon: "progress"},
      {path: "/settings", label: "设置与隐私", icon: "settings"}
    ];
    const navigationIcons = {
      workbench: [["path", {d: "M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v9a2.5 2.5 0 0 1-2.5 2.5H10l-5 4v-4.7A2.5 2.5 0 0 1 4 14.5Z"}], ["path", {d: "M8 8h8M8 12h5"}]],
      errors: [["path", {d: "M5 4.5A2.5 2.5 0 0 1 7.5 2H19v18H7.5A2.5 2.5 0 0 0 5 22Z"}], ["path", {d: "M5 4.5v15M9 7h6M9 11h6M9 15h4"}]],
      reviews: [["circle", {cx: 12, cy: 12, r: 9}], ["path", {d: "M12 7v5l3 2M8 12l2 2 4-4"}]],
      practice: [["path", {d: "M6 2h8l4 4v16H6Z"}], ["path", {d: "M14 2v5h5M9 12h6M9 16h6"}]],
      progress: [["path", {d: "M4 20V10M10 20V4M16 20v-7M22 20H2"}]],
      settings: [["circle", {cx: 12, cy: 12, r: 3}], ["path", {d: "M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"}]]
    };

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
          [data-lzlm-product-nav] {
            display: flex;
            width: 100%;
            flex-direction: column;
            gap: 2px;
            padding: 8px 0;
            border-top: 1px solid var(--dsw-alias-border-l3);
          }
          [data-lzlm-product-nav] a {
            display: flex;
            min-height: 36px;
            align-items: center;
            gap: 10px;
            padding: 0 10px;
            border-radius: 8px;
            color: inherit;
            text-decoration: none;
          }
          [data-lzlm-product-nav] a:hover,
          [data-lzlm-product-nav] a[aria-current="page"] {
            background: var(--dsw-alias-interactive-bg-hover);
          }
          [data-lzlm-product-nav] svg {
            width: 18px;
            height: 18px;
            flex: none;
            fill: none;
            stroke: currentColor;
            stroke-width: 1.6;
            stroke-linecap: round;
            stroke-linejoin: round;
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

    function NavIcon({name}) {
      return jsx("svg", {
        viewBox: "0 0 24 24",
        "aria-hidden": "true",
        children: navigationIcons[name].map(([tag, props], index) => jsx(tag, props, index))
      });
    }

    function ProductNavigation({wide}) {
      return jsx("nav", {
        "aria-label": "错题本功能导航",
        "data-lzlm-product-nav": "",
        children: navigationItems.map((item) => jsx("a", {
          href: `${productOrigin}${item.path}`,
          target: "_top",
          title: item.label,
          "aria-current": item.path === "/" ? "page" : undefined,
          style: wide ? undefined : {justifyContent: "center", padding: 0},
          children: [jsx(NavIcon, {name: item.icon}, "icon"), wide ? jsx("span", {children: item.label}, "label") : null]
        }, item.path))
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
                id: "math-notebook-navigation",
                order: 100,
                label: "错题本功能导航"
              }, ProductNavigation);
            }))));
    }

    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  }
});
