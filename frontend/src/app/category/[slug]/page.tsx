import type { Metadata } from "next";
import { Feed } from "@/components/Feed";
import { StaleNotice } from "@/components/StaleNotice";
import { fetchArticlesSafe, fetchCategories } from "@/lib/api";

async function categoryName(slug: string): Promise<string | null> {
  const categories = await fetchCategories();
  return categories.find((c) => c.slug === slug)?.name ?? null;
}

export async function generateMetadata({
  params,
}: {
  params: { slug: string };
}): Promise<Metadata> {
  const name = await categoryName(params.slug);
  return { title: name ?? "Category" };
}

export default async function CategoryPage({
  params,
  searchParams,
}: {
  params: { slug: string };
  searchParams: { cursor?: string };
}) {
  const [name, { page, failed }] = await Promise.all([
    categoryName(params.slug),
    fetchArticlesSafe({ category: params.slug, cursor: searchParams.cursor }),
  ]);

  const heading = name ?? params.slug.replace(/-/g, " ");

  return (
    <>
      <h1 className="page-title" style={{ textTransform: name ? "none" : "capitalize" }}>
        {heading}
      </h1>
      <p className="page-sub">Stories classified into this category.</p>

      <StaleNotice latestPublishedAt={page.articles[0]?.publishedAt ?? null} />

      <Feed
        page={page}
        failed={failed}
        emptyMessage={`Nothing classified as ${heading} in the current window.`}
        moreHref={`/category/${params.slug}?`}
      />
    </>
  );
}
