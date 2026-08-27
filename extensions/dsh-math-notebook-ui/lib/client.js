window.__ModuleLoader__.load({
  id: "@lizhaolin/dsh-math-notebook-ui",
  factory: (require) => {
    const module = {exports: {}};
    const exports = module.exports;
    const {jsx} = require("react/jsx-runtime");
    const productOrigin = "http://127.0.0.1:8000";
    const productWorkspaceTitle = "错题会话";
    const navigationItems = [
      {path: "/errors", label: "错题本", icon: "errors"},
      {path: "/reviews", label: "今日复习", icon: "reviews"},
      {path: "/practice", label: "练习 PDF", icon: "practice"},
      {path: "/progress", label: "学习进度", icon: "progress"}
    ];
    const navigationIcons = {
      errors: [["path", {d: "M5 4.5A2.5 2.5 0 0 1 7.5 2H19v18H7.5A2.5 2.5 0 0 0 5 22Z"}], ["path", {d: "M5 4.5v15M9 7h6M9 11h6M9 15h4"}]],
      reviews: [["circle", {cx: 12, cy: 12, r: 9}], ["path", {d: "M12 7v5l3 2M8 12l2 2 4-4"}]],
      practice: [["path", {d: "M6 2h8l4 4v16H6Z"}], ["path", {d: "M14 2v5h5M9 12h6M9 16h6"}]],
      progress: [["path", {d: "M4 20V10M10 20V4M16 20v-7M22 20H2"}]]
    };
    let activeProductPath = null;
    let disposeProductSurface = null;

    function updateProductNavigation() {
      document.querySelectorAll("[data-lzlm-product-path]").forEach((element) => {
        const active = element.dataset.lzlmProductPath === activeProductPath;
        if (active) element.setAttribute("aria-current", "page");
        else element.removeAttribute("aria-current");
      });
    }

    function closeProductSurface() {
      if (disposeProductSurface === null) return;
      const dispose = disposeProductSurface;
      disposeProductSurface = null;
      activeProductPath = null;
      dispose();
      updateProductNavigation();
    }

    function ProductSurface() {
      const item = navigationItems.find(({path}) => path === activeProductPath);
      if (item === undefined) return null;
      return jsx("div", {
        "data-lzlm-product-surface": "",
        children: jsx("iframe", {
          src: `${productOrigin}${item.path}?embedded=1`,
          title: item.label
        })
      });
    }

    function openProductSurface(ctx, path) {
      if (activeProductPath === path && disposeProductSurface !== null) return;
      closeProductSurface();
      activeProductPath = path;
      try {
        disposeProductSurface = ctx.slots.register({name: "conversation", priority: -1}, ProductSurface);
      } catch (error) {
        activeProductPath = null;
        updateProductNavigation();
        throw error;
      }
      updateProductNavigation();
    }

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
          [data-lzlm-product-nav] button {
            display: flex;
            width: 100%;
            min-height: 36px;
            align-items: center;
            gap: 10px;
            padding: 0 10px;
            border: 0;
            border-radius: 8px;
            background: transparent;
            color: inherit;
            font: inherit;
            text-decoration: none;
            cursor: pointer;
            text-align: left;
          }
          [data-lzlm-product-nav] button:hover,
          [data-lzlm-product-nav] button[aria-current="page"] {
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
          [data-lzlm-product-surface] {
            width: 100%;
            height: 100%;
            min-width: 0;
            min-height: 0;
            background: #f6f5f1;
          }
          [data-lzlm-product-surface] iframe {
            display: block;
            width: 100%;
            height: 100%;
            border: 0;
          }
          [data-lzlm-account-privacy] {
            max-width: 640px;
            padding: 8px 4px;
          }
          [data-lzlm-account-privacy] h2 {
            margin: 0 0 10px;
            font-size: 20px;
          }
          [data-lzlm-account-privacy] p {
            margin: 0 0 18px;
            color: var(--dsw-alias-label-secondary);
            line-height: 1.7;
          }
          [data-lzlm-account-privacy] a {
            display: inline-flex;
            min-height: 36px;
            align-items: center;
            padding: 0 14px;
            border: 1px solid var(--dsw-alias-border-l2);
            border-radius: 8px;
            color: inherit;
            text-decoration: none;
          }
          [data-lzlm-account-privacy] a:hover {
            background: var(--dsw-alias-interactive-bg-hover);
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
        alt: "",
        onClick: closeProductSurface
      });
    }

    function BrandName() {
      return jsx("span", {
        style: {fontSize: "14px", fontWeight: 700, whiteSpace: "nowrap", cursor: "pointer"},
        onClick: closeProductSurface,
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
        children: navigationItems.map((item) => jsx("button", {
          type: "button",
          "data-lzlm-product-path": item.path,
          "aria-current": activeProductPath === item.path ? "page" : undefined,
          onClick: () => openProductSurface(pluginContext, item.path),
          title: item.label,
          style: wide ? undefined : {justifyContent: "center", padding: 0},
          children: [jsx(NavIcon, {name: item.icon}, "icon"), wide ? jsx("span", {children: item.label}, "label") : null]
        }, item.path))
      });
    }

    function AccountPrivacySettings() {
      return jsx("section", {
        "data-lzlm-account-privacy": "",
        children: [
          jsx("h2", {children: "账号与隐私"}, "title"),
          jsx("p", {children: "管理当前账号会话、退出所有设备、导出个人数据或注销账号。敏感操作仍需要手机号验证码确认。"}, "description"),
          jsx("a", {href: `${productOrigin}/settings`, target: "_top", children: "管理账号与隐私"}, "action")
        ]
      });
    }

    let pluginContext;
    const inject = ["slots", "sessions", "workspaces"];
    function apply(ctx) {
      pluginContext = ctx;
      installStudentSurface(ctx);
      openProductWorkspace(ctx);
      ctx.effect(() => {
        let current = ctx.sessions.list.getSnapshot().current;
        return ctx.sessions.list.subscribe(() => {
          const next = ctx.sessions.list.getSnapshot().current;
          if (next !== current) closeProductSurface();
          current = next;
        });
      }, "math-notebook: close product page on session navigation");
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
      ctx.slots.inject("settings.section", () => ctx.slots.register({
        name: "settings.section",
        id: "account-privacy",
        order: 20,
        label: "账号与隐私"
      }, AccountPrivacySettings));
    }

    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  }
});
