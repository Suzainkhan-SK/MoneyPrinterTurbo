import os
import glob
import streamlit

def patch():
    static_dir = os.path.join(os.path.dirname(streamlit.__file__), "static")
    index_path = os.path.join(static_dir, "index.html")
    
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()
            
        # 1. Replace title tag
        html = html.replace("<title>Streamlit</title>", "<title>⚡ Bang AI</title>")
        html = html.replace("<title>Bang AI</title>", "<title>⚡ Bang AI</title>")
        
        # 2. Inject script to override document.title & meta tags
        script_snippet = """
    <!-- Bang AI Title & Metadata Sanitizer -->
    <script>
    (function() {
      function clean(val) {
        if (!val || typeof val !== 'string') return val;
        return val.replace(/\\s*[·•\\-]\\s*Streamlit/gi, '').replace(/Streamlit/gi, 'Bang AI').trim();
      }
      var currentTitle = '⚡ Bang AI';
      try {
        Object.defineProperty(document, 'title', {
          get: function() {
            return currentTitle;
          },
          set: function(val) {
            currentTitle = clean(val) || '⚡ Bang AI';
            var el = document.querySelector('title');
            if (el) el.textContent = currentTitle;
          },
          configurable: true
        });
      } catch (e) {}

      function cleanMeta() {
        if (document.title && document.title.includes('Streamlit')) {
          document.title = clean(document.title);
        }
        document.querySelectorAll('meta').forEach(function(m) {
          ['content', 'name', 'property'].forEach(function(attr) {
            var v = m.getAttribute(attr);
            if (v && v.includes('Streamlit')) {
              m.setAttribute(attr, clean(v));
            }
          });
          if (m.getAttribute('name') === 'generator') {
            m.setAttribute('content', 'Bang AI Engine');
          }
        });
      }
      cleanMeta();
      document.addEventListener('DOMContentLoaded', cleanMeta);
      window.addEventListener('load', cleanMeta);
      setInterval(cleanMeta, 300);
    })();
    </script>
        """
        
        if "Bang AI Title & Metadata Sanitizer" not in html:
            html = html.replace("</head>", f"{script_snippet}\n  </head>")
            
        # 3. Replace favicon
        svg_favicon = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">⚡</text></svg>'
        html = html.replace('<link rel="shortcut icon" href="./favicon.png" />', f'<link rel="shortcut icon" href="{svg_favicon}" />\n    <link rel="icon" href="{svg_favicon}" />')
        
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(html)
        print("Successfully patched index.html")

    # 4. Patch JS files for fallback title
    js_files = glob.glob(os.path.join(static_dir, "**", "*.js"), recursive=True)
    patched_js_count = 0
    for js_path in js_files:
        try:
            with open(js_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            changed = False
            if "`Streamlit`" in content:
                content = content.replace("`Streamlit`", "`Bang AI`")
                changed = True
            if '"Streamlit"' in content:
                content = content.replace('"Streamlit"', '"Bang AI"')
                changed = True
            if "· Streamlit" in content:
                content = content.replace("· Streamlit", "")
                changed = True
            if changed:
                with open(js_path, "w", encoding="utf-8") as f:
                    f.write(content)
                patched_js_count += 1
        except Exception as e:
            print(f"Error patching {js_path}: {e}")
            
    # 5. Overwrite favicon.png with Bang AI icon
    try:
        from PIL import Image, ImageDraw
        fav_path = os.path.join(static_dir, "favicon.png")
        img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([0, 0, 31, 31], radius=7, fill=(255, 79, 0, 255))
        d.polygon([(18, 4), (10, 16), (16, 16), (14, 28), (22, 14), (16, 14)], fill=(255, 254, 251, 255))
        img.save(fav_path, "PNG")
        print(f"Generated custom Bang AI favicon at {fav_path}")
    except Exception as e:
        print(f"Could not generate favicon.png: {e}")

if __name__ == "__main__":
    patch()
