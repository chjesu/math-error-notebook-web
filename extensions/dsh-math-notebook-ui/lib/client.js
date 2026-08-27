window.__ModuleLoader__.load({
  id: "@lizhaolin/dsh-math-notebook-ui",
  factory: (require) => {
    const module = {exports: {}};
    const exports = module.exports;
    const {jsx} = require("react/jsx-runtime");
    const productOrigin = "http://127.0.0.1:8000";

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

    const inject = ["slots"];
    function apply(ctx) {
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
