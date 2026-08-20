import { useCallback, useEffect, useState } from "react";

const REFRESH_MS = 60_000;

async function api(path, options) {
  const resp = await fetch(path, options);
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`;
    try {
      const body = await resp.json();
      if (body.detail) detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return resp.json();
}

function relTime(iso) {
  if (!iso) return "never";
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} hr${hours === 1 ? "" : "s"} ago`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

const STATUS = {
  in_stock: { label: "In stock", className: "badge in-stock" },
  out_of_stock: { label: "Out of stock", className: "badge out-of-stock" },
};

function statusInfo(status) {
  return STATUS[status] ?? { label: "Unknown", className: "badge unknown" };
}

function ProductCard({ product, onRemove }) {
  const [removing, setRemoving] = useState(false);
  const status = statusInfo(product.status);

  async function handleRemove() {
    if (!window.confirm(`Stop tracking "${product.name}"?`)) return;
    setRemoving(true);
    try {
      await onRemove(product.url);
    } finally {
      setRemoving(false);
    }
  }

  return (
    <div className="card">
      {product.image_url ? (
        <img className="thumb" src={product.image_url} alt="" loading="lazy" />
      ) : (
        <div className="thumb placeholder">📦</div>
      )}
      <div className="card-body">
        <a className="name" href={product.url} target="_blank" rel="noreferrer">
          {product.name}
        </a>
        <div className="meta">
          <span className={status.className}>{status.label}</span>
          {product.price && <span className="price">{product.price}</span>}
        </div>
        <div className="fine">
          Checked {relTime(product.last_checked)} · Added by{" "}
          {product.added_by_name}
        </div>
      </div>
      <button
        className="remove"
        onClick={handleRemove}
        disabled={removing}
        title="Stop tracking"
      >
        ✕
      </button>
    </div>
  );
}

export default function App() {
  const [products, setProducts] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [newUrl, setNewUrl] = useState("");
  const [adding, setAdding] = useState(false);
  const [checking, setChecking] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setProducts(await api("/api/products"));
      setError(null);
    } catch (err) {
      setError(`Couldn't reach the API: ${err.message}`);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  async function handleAdd(event) {
    event.preventDefault();
    if (!newUrl.trim() || adding) return;
    setAdding(true);
    setError(null);
    setNotice("Adding — fetching the product page…");
    try {
      const added = await api("/api/products", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: newUrl.trim() }),
      });
      setNewUrl("");
      setNotice(`Now tracking “${added.name}”.`);
      await refresh();
    } catch (err) {
      setNotice(null);
      setError(err.message);
    } finally {
      setAdding(false);
    }
  }

  async function handleRemove(url) {
    setError(null);
    try {
      await api(`/api/products?url=${encodeURIComponent(url)}`, {
        method: "DELETE",
      });
      await refresh();
    } catch (err) {
      setError(err.message);
    }
  }

  async function handleCheckNow() {
    setChecking(true);
    setError(null);
    setNotice("Checking every tracked product — this can take a while…");
    try {
      const result = await api("/api/check", { method: "POST" });
      const hits = result.newly_in_stock.length;
      setNotice(
        hits
          ? `Done — ${hits} item(s) came back in stock! Discord pings are on the way.`
          : `Done — checked ${result.checked} product(s), no new stock.`
      );
      await refresh();
    } catch (err) {
      setNotice(null);
      setError(err.message);
    } finally {
      setChecking(false);
    }
  }

  const inStock = products?.filter((p) => p.status === "in_stock").length ?? 0;

  return (
    <div className="page">
      <header>
        <h1>
          StockScout <span className="tagline">stock-alert dashboard</span>
        </h1>
        {products && (
          <div className="counts">
            {products.length} tracked · {inStock} in stock
          </div>
        )}
      </header>

      <form className="add-form" onSubmit={handleAdd}>
        <input
          type="url"
          placeholder="https://store.example.com/product…"
          value={newUrl}
          onChange={(event) => setNewUrl(event.target.value)}
          disabled={adding}
          required
        />
        <button type="submit" disabled={adding}>
          {adding ? "Adding…" : "Track product"}
        </button>
        <button
          type="button"
          className="secondary"
          onClick={handleCheckNow}
          disabled={checking || !products?.length}
        >
          {checking ? "Checking…" : "Check all now"}
        </button>
      </form>

      {error && <div className="banner error">{error}</div>}
      {notice && !error && <div className="banner">{notice}</div>}

      {products === null ? (
        <p className="empty">Loading…</p>
      ) : products.length === 0 ? (
        <p className="empty">
          Nothing is being tracked yet. Paste a product URL above to start.
        </p>
      ) : (
        <div className="grid">
          {products.map((product) => (
            <ProductCard
              key={product.url_key}
              product={product}
              onRemove={handleRemove}
            />
          ))}
        </div>
      )}
    </div>
  );
}
