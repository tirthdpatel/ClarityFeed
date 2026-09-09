import type { Metadata } from "next";
import { DatePicker } from "@/components/DatePicker";
import { Feed } from "@/components/Feed";
import { StaleNotice } from "@/components/StaleNotice";
import { fetchArchive, fetchArticlesSafe, fetchCategories } from "@/lib/api";

async function categoryName(slug: string): Promise<string | null> {
  const categories = await fetchCategories();
  return categories.find((c) => c.slug === slug)?.name ?? null;
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ slug: string }>;
}): Promise<Metadata> {
  const { slug } = await params;
  const name = await categoryName(slug);
  return { title: name ?? "Category" };
}

export default async function CategoryPage({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<{ cursor?: string; date?: string }>;
}) {
  const [{ slug }, { cursor, date }] = await Promise.all([params, searchParams]);
  const [name, { page, failed }] = await Promise.all([
    categoryName(slug),
    fetchArticlesSafe({ category: slug, cursor, date }),
  ]);
  const archive = await fetchArchive();

  const heading = name ?? slug.replace(/-/g, " ");

  return (
    <>
      <h1 className="page-title" style={{ textTransform: name ? "none" : "capitalize" }}>
        {heading}
      </h1>
      <p className="page-sub">Stories classified into this category.</p>

      <DatePicker archive={archive} selected={date} basePath={`/category/${slug}`} />

      {date ? null : (
        <StaleNotice latestPublishedAt={page.articles[0]?.publishedAt ?? null} />
      )}

      <Feed
        page={page}
        failed={failed}
        emptyMessage={`Nothing classified as ${heading} in the current window.`}
        moreHref={date ? `/category/${slug}?date=${date}&` : `/category/${slug}?`}
      />
    </>
  );
}
