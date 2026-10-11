<script setup lang="ts">
import { ErrorMessage, Skeleton } from 'frappe-ui'
import { useTranslation } from '../translation'

defineProps<{ error?: Error }>()

const __ = useTranslation()
</script>

<template>
  <div class="px-4 pt-6 sm:px-10 sm:pt-9">
    <ErrorMessage
      v-if="error"
      :message="__('Could not load this panel. Check your connection, then open Cloud Settings again.')"
    />

    <div v-else class="space-y-3" role="status" :aria-label="__('Loading')">
      <Skeleton class="h-7 w-40 rounded-4" />
      <Skeleton v-for="n in 3" :key="n" class="h-24 rounded-6" />
    </div>
  </div>
</template>
